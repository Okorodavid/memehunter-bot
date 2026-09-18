"""SQLite persistence: tracked wallets, seen positions, alert dedupe, HTTP cache."""
from __future__ import annotations

import json
import time
from typing import Any, Iterable

import aiosqlite

from .config import CFG

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracked_wallets (
    chat_id      INTEGER NOT NULL,
    chain        TEXT    NOT NULL,
    address      TEXT    NOT NULL,
    label        TEXT,
    source_token TEXT,
    added_at     INTEGER NOT NULL,
    -- 1 when this row exists only because the wallet was expanded across every
    -- EVM chain, rather than being tracked on this chain deliberately.
    auto         INTEGER NOT NULL DEFAULT 0,
    -- A wallet with no token history on a chain is parked so we stop paying to
    -- poll it, and re-probed on a timer in case it starts using that chain later.
    dormant      INTEGER NOT NULL DEFAULT 0,
    empty_streak INTEGER NOT NULL DEFAULT 0,
    last_probe   INTEGER,
    PRIMARY KEY (chat_id, chain, address)
);

-- Wallet-chain pairs we have taken a baseline for.
--
-- This is deliberately separate from wallet_positions. A wallet that holds
-- NOTHING on a chain is a perfectly valid baseline, and "has rows in
-- wallet_positions" cannot express it -- which meant an empty wallet was
-- re-seeded on every poll and its first buy never alerted. That case is the
-- normal one once a wallet is watched across every chain.
CREATE TABLE IF NOT EXISTS wallet_seen (
    chain     TEXT    NOT NULL,
    wallet    TEXT    NOT NULL,
    seeded_at INTEGER NOT NULL,
    PRIMARY KEY (chain, wallet)
);

-- Balance of every token we have seen a wallet hold. Storing the amount (not just
-- the fact of the position) is what makes selling visible: a shrinking balance is a
-- trim, a balance that goes to dust is an exit. A closed position stays here at
-- amount 0 so that buying back in later reads as a genuinely new entry.
CREATE TABLE IF NOT EXISTS wallet_positions (
    chain      TEXT    NOT NULL,
    wallet     TEXT    NOT NULL,
    token      TEXT    NOT NULL,
    amount     REAL    NOT NULL DEFAULT 0,
    peak       REAL    NOT NULL DEFAULT 0,
    first_seen INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    PRIMARY KEY (chain, wallet, token)
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    chat_id INTEGER NOT NULL,
    chain   TEXT    NOT NULL,
    wallet  TEXT    NOT NULL,
    token   TEXT    NOT NULL,
    kind    TEXT    NOT NULL DEFAULT 'BUY',
    sent_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, chain, wallet, token, kind)
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id   INTEGER PRIMARY KEY,
    watch     INTEGER NOT NULL DEFAULT 0,
    min_score REAL,
    bankroll  REAL,
    sell_alerts INTEGER NOT NULL DEFAULT 1,
    autoscan    INTEGER NOT NULL DEFAULT 0,
    autoharvest INTEGER NOT NULL DEFAULT 0
);

-- What the last automatic scan already told you about each token. An interval
-- scan that re-sent the same overlap every cycle would be noise, so a token is
-- only reported again when it gains a wallet or its score moves materially.
CREATE TABLE IF NOT EXISTS scan_reports (
    chat_id     INTEGER NOT NULL,
    chain       TEXT    NOT NULL,
    token       TEXT    NOT NULL,
    wallets     INTEGER NOT NULL,
    score       REAL    NOT NULL,
    reported_at INTEGER NOT NULL,
    PRIMARY KEY (chat_id, chain, token)
);

-- Every entry alert, plus what the price did afterwards. This is the only
-- honest way to rank a wallet: not by what it once made, but by how the calls
-- it gave YOU actually turned out.
CREATE TABLE IF NOT EXISTS alert_outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    chain       TEXT    NOT NULL,
    wallet      TEXT    NOT NULL,
    token       TEXT    NOT NULL,
    symbol      TEXT,
    kind        TEXT    NOT NULL,
    score       REAL,
    entry_price REAL    NOT NULL,
    peak_price  REAL    NOT NULL,
    last_price  REAL    NOT NULL,
    opened_at   INTEGER NOT NULL,
    last_check  INTEGER NOT NULL,
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outcomes_open ON alert_outcomes(closed, last_check);
CREATE INDEX IF NOT EXISTS idx_outcomes_wallet ON alert_outcomes(chat_id, wallet);

-- Coins waiting to be mined for early buyers. Step 1 of the method is a human
-- decision, so this is a queue you fill; the harvester just works through it.
CREATE TABLE IF NOT EXISTS harvest_queue (
    chat_id  INTEGER NOT NULL,
    token    TEXT    NOT NULL,
    symbol   TEXT,
    source   TEXT,
    added_at INTEGER NOT NULL,
    done_at  INTEGER,
    status   TEXT    NOT NULL DEFAULT 'pending',
    note     TEXT,
    PRIMARY KEY (chat_id, token)
);

CREATE TABLE IF NOT EXISTS cache (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cache_exp ON cache(expires_at);
"""


async def _columns(conn, table: str) -> set[str]:
    rows = await (await conn.execute(f"PRAGMA table_info({table})")).fetchall()
    return {r["name"] for r in rows}


async def _migrate(conn: aiosqlite.Connection) -> None:
    """Bring a database created by an earlier version up to the current schema."""
    positions = await _columns(conn, "wallet_positions")
    for column in ("amount", "peak"):
        if column not in positions:
            await conn.execute(
                f"ALTER TABLE wallet_positions ADD COLUMN {column} REAL NOT NULL DEFAULT 0")

    settings = await _columns(conn, "chat_settings")
    if "bankroll" not in settings:
        await conn.execute("ALTER TABLE chat_settings ADD COLUMN bankroll REAL")
    if "sell_alerts" not in settings:
        await conn.execute(
            "ALTER TABLE chat_settings ADD COLUMN sell_alerts INTEGER NOT NULL DEFAULT 1")
    for flag in ("autoscan", "autoharvest"):
        if flag not in settings:
            await conn.execute(
                f"ALTER TABLE chat_settings ADD COLUMN {flag} INTEGER NOT NULL DEFAULT 0")

    tracked = await _columns(conn, "tracked_wallets")
    for column in ("auto", "dormant", "empty_streak"):
        if column not in tracked:
            await conn.execute(
                f"ALTER TABLE tracked_wallets ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0")
    if "last_probe" not in tracked:
        await conn.execute("ALTER TABLE tracked_wallets ADD COLUMN last_probe INTEGER")

    # Backfill the baseline marker from positions we already hold, so upgrading
    # does not make every existing wallet look unseeded and swallow a poll.
    await conn.execute(
        "INSERT OR IGNORE INTO wallet_seen (chain, wallet, seeded_at)"
        " SELECT chain, wallet, MIN(first_seen) FROM wallet_positions"
        " GROUP BY chain, wallet"
    )

    # The alert key gained an event kind, and a primary key cannot be altered in
    # place. This table is pure dedupe state, so rebuilding it costs nothing.
    if "kind" not in await _columns(conn, "alerts_sent"):
        await conn.execute("DROP TABLE alerts_sent")
        await conn.executescript(SCHEMA)
    await conn.commit()


async def connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(CFG.db_path)
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    await _migrate(conn)
    await conn.commit()
    return conn


# --------------------------------------------------------------------------- cache
async def cache_get(conn: aiosqlite.Connection, key: str) -> Any | None:
    row = await (await conn.execute(
        "SELECT value FROM cache WHERE key = ? AND expires_at > ?", (key, int(time.time()))
    )).fetchone()
    return json.loads(row["value"]) if row else None


async def cache_set(conn: aiosqlite.Connection, key: str, value: Any, ttl: int) -> None:
    await conn.execute(
        "INSERT OR REPLACE INTO cache (key, value, expires_at) VALUES (?, ?, ?)",
        (key, json.dumps(value), int(time.time()) + ttl),
    )
    await conn.commit()


async def cache_sweep(conn: aiosqlite.Connection) -> None:
    await conn.execute("DELETE FROM cache WHERE expires_at <= ?", (int(time.time()),))
    await conn.commit()


# ------------------------------------------------------------------ tracked wallets
async def add_wallet(conn, chat_id: int, chain: str, address: str,
                     label: str | None, source_token: str | None,
                     auto: bool = False) -> bool:
    """Returns True when the wallet was newly added, False when already tracked."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO tracked_wallets"
        " (chat_id, chain, address, label, source_token, added_at, auto)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, chain, address, label, source_token, int(time.time()),
         1 if auto else 0),
    )
    await conn.commit()
    return cur.rowcount > 0


async def record_probe(conn, chat_id: int, chain: str, address: str,
                       had_history: bool, dormant_after: int) -> bool:
    """Log whether the wallet shows any token history on this chain.

    Returns True if the row is now dormant. A chain is only parked after several
    consecutive empty reads, because Etherscan returns an empty list for both
    "no transactions" and "request failed" -- parking on one empty answer would
    silently stop watching a chain over a blip.
    """
    if had_history:
        await conn.execute(
            "UPDATE tracked_wallets SET empty_streak = 0, dormant = 0, last_probe = ?"
            " WHERE chat_id = ? AND chain = ? AND address = ?",
            (int(time.time()), chat_id, chain, address),
        )
        await conn.commit()
        return False

    await conn.execute(
        "UPDATE tracked_wallets SET empty_streak = empty_streak + 1, last_probe = ?,"
        " dormant = CASE WHEN empty_streak + 1 >= ? THEN 1 ELSE dormant END"
        " WHERE chat_id = ? AND chain = ? AND address = ?",
        (int(time.time()), dormant_after, chat_id, chain, address),
    )
    await conn.commit()
    row = await (await conn.execute(
        "SELECT dormant FROM tracked_wallets"
        " WHERE chat_id = ? AND chain = ? AND address = ?",
        (chat_id, chain, address),
    )).fetchone()
    return bool(row and row["dormant"])


async def wallets_to_poll(conn, chat_id: int, reprobe_after_seconds: int,
                          probe_budget: int) -> list[aiosqlite.Row]:
    """Rows the watcher should read this cycle.

    Every live wallet-chain pair, plus a bounded slice of dormant ones that are
    due a re-probe. The slice is what keeps watching eight chains affordable:
    after the first pass most pairs are parked, and only a handful are retried
    per cycle. Without the re-probe a wallet that starts using a new chain next
    month would never be noticed again.
    """
    live = list(await (await conn.execute(
        "SELECT * FROM tracked_wallets WHERE chat_id = ? AND dormant = 0"
        " ORDER BY added_at DESC",
        (chat_id,),
    )).fetchall())

    due = list(await (await conn.execute(
        "SELECT * FROM tracked_wallets WHERE chat_id = ? AND dormant = 1"
        " AND (last_probe IS NULL OR last_probe < ?)"
        " ORDER BY COALESCE(last_probe, 0) ASC LIMIT ?",
        (chat_id, int(time.time()) - reprobe_after_seconds, max(probe_budget, 0)),
    )).fetchall())
    return live + due


async def wallet_chains(conn, chat_id: int, address: str) -> list[aiosqlite.Row]:
    return list(await (await conn.execute(
        "SELECT * FROM tracked_wallets WHERE chat_id = ? AND lower(address) = lower(?)"
        " ORDER BY dormant ASC, chain ASC",
        (chat_id, address),
    )).fetchall())


async def set_label(conn, chat_id: int, address: str, label: str) -> int:
    cur = await conn.execute(
        "UPDATE tracked_wallets SET label = ? WHERE chat_id = ? AND lower(address) = lower(?)",
        (label, chat_id, address),
    )
    await conn.commit()
    return cur.rowcount


async def remove_wallet(conn, chat_id: int, address: str) -> int:
    cur = await conn.execute(
        "DELETE FROM tracked_wallets WHERE chat_id = ? AND lower(address) = lower(?)",
        (chat_id, address),
    )
    await conn.commit()
    return cur.rowcount


async def list_wallets(conn, chat_id: int) -> list[aiosqlite.Row]:
    return list(await (await conn.execute(
        "SELECT * FROM tracked_wallets WHERE chat_id = ? ORDER BY added_at DESC", (chat_id,)
    )).fetchall())


async def all_watching_chats(conn) -> list[int]:
    rows = await (await conn.execute(
        "SELECT chat_id FROM chat_settings WHERE watch = 1"
    )).fetchall()
    return [r["chat_id"] for r in rows]


# ---------------------------------------------------------------------- positions
async def known_positions(conn, chain: str, wallet: str) -> set[str]:
    """Tokens the wallet currently holds a non-zero balance of, as far as we know."""
    rows = await (await conn.execute(
        "SELECT token FROM wallet_positions"
        " WHERE chain = ? AND wallet = ? AND amount > 0", (chain, wallet)
    )).fetchall()
    return {r["token"] for r in rows}


async def position_snapshot(conn, chain: str, wallet: str) -> dict[str, dict]:
    """Last known balance and all-time peak for each token, keyed by token."""
    rows = await (await conn.execute(
        "SELECT token, amount, peak FROM wallet_positions WHERE chain = ? AND wallet = ?",
        (chain, wallet),
    )).fetchall()
    return {r["token"]: {"amount": r["amount"], "peak": r["peak"]} for r in rows}


async def has_snapshot(conn, chain: str, wallet: str) -> bool:
    """Have we taken a baseline for this wallet on this chain?

    Asks wallet_seen rather than wallet_positions: a wallet that holds nothing
    here is still baselined, and answering from position rows alone would make
    us re-seed it forever and never alert on its first buy.
    """
    row = await (await conn.execute(
        "SELECT 1 FROM wallet_seen WHERE chain = ? AND wallet = ? LIMIT 1",
        (chain, wallet),
    )).fetchone()
    return row is not None


async def mark_seeded(conn, chain: str, wallet: str) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO wallet_seen (chain, wallet, seeded_at) VALUES (?, ?, ?)",
        (chain, wallet, int(time.time())),
    )
    await conn.commit()


async def save_snapshot(conn, chain: str, wallet: str,
                        balances: dict[str, float]) -> None:
    """Write the wallet's current balances, zeroing anything it no longer holds.

    Peak is kept so a partial sell is measured against the largest the position ever
    was, rather than against whatever it happened to be on the previous poll.
    """
    now = int(time.time())
    if balances:
        await conn.executemany(
            "INSERT INTO wallet_positions"
            " (chain, wallet, token, amount, peak, first_seen, last_seen)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(chain, wallet, token) DO UPDATE SET"
            "   amount = excluded.amount,"
            "   peak = MAX(wallet_positions.peak, excluded.amount),"
            "   last_seen = excluded.last_seen",
            [(chain, wallet, token, amount, amount, now, now)
             for token, amount in balances.items()],
        )
        # Anything previously held but absent from this snapshot has been closed out.
        placeholders = ",".join(["?"] * len(balances))
        await conn.execute(
            "UPDATE wallet_positions SET amount = 0, last_seen = ?"
            " WHERE chain = ? AND wallet = ? AND amount > 0"
            f" AND token NOT IN ({placeholders})",
            (now, chain, wallet, *balances.keys()),
        )
    else:
        await conn.execute(
            "UPDATE wallet_positions SET amount = 0, last_seen = ?"
            " WHERE chain = ? AND wallet = ? AND amount > 0",
            (now, chain, wallet),
        )
    # Saving a snapshot IS taking a baseline, including a snapshot of nothing.
    # Recording it here rather than at each call site is what stops an empty
    # wallet being re-seeded forever and its first buy going unreported.
    await conn.execute(
        "INSERT OR IGNORE INTO wallet_seen (chain, wallet, seeded_at) VALUES (?, ?, ?)",
        (chain, wallet, now),
    )
    await conn.commit()


async def record_positions(conn, chain: str, wallet: str, tokens: Iterable[str]) -> None:
    """Seed-only helper for when balances are not known, just the set of tokens."""
    now = int(time.time())
    await conn.executemany(
        "INSERT INTO wallet_positions"
        " (chain, wallet, token, amount, peak, first_seen, last_seen)"
        " VALUES (?, ?, ?, 0, 0, ?, ?)"
        " ON CONFLICT(chain, wallet, token) DO UPDATE SET last_seen = excluded.last_seen",
        [(chain, wallet, t, now, now) for t in tokens],
    )
    await conn.commit()


# ------------------------------------------------------------------------- alerts
COMPLEMENTS = {
    "BUY": ("TRIM", "EXIT"),
    "ADD": ("TRIM", "EXIT"),
    "TRIM": ("BUY", "ADD"),
    "EXIT": ("BUY", "ADD", "TRIM"),
}


async def should_alert(conn, chat_id: int, chain: str, wallet: str, token: str,
                       kind: str = "BUY") -> bool:
    """True the first time this event fires for this position.

    Firing one kind clears its opposites, so a wallet that buys, exits, then buys
    back in alerts on every leg, while a poll that sees no change stays silent.
    """
    cur = await conn.execute(
        "INSERT OR IGNORE INTO alerts_sent (chat_id, chain, wallet, token, kind, sent_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, chain, wallet, token, kind, int(time.time())),
    )
    fresh = cur.rowcount > 0
    if fresh:
        opposites = COMPLEMENTS.get(kind, ())
        if opposites:
            marks = ",".join(["?"] * len(opposites))
            await conn.execute(
                "DELETE FROM alerts_sent WHERE chat_id = ? AND chain = ? AND wallet = ?"
                f" AND token = ? AND kind IN ({marks})",
                (chat_id, chain, wallet, token, *opposites),
            )
    await conn.commit()
    return fresh


# ----------------------------------------------------------------------- settings
async def get_settings(conn, chat_id: int) -> aiosqlite.Row | None:
    return await (await conn.execute(
        "SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)
    )).fetchone()


async def set_watch(conn, chat_id: int, on: bool) -> None:
    await conn.execute(
        "INSERT INTO chat_settings (chat_id, watch) VALUES (?, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET watch = excluded.watch",
        (chat_id, 1 if on else 0),
    )
    await conn.commit()


async def set_bankroll(conn, chat_id: int, amount: float) -> None:
    await conn.execute(
        "INSERT INTO chat_settings (chat_id, watch, bankroll) VALUES (?, 0, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET bankroll = excluded.bankroll",
        (chat_id, amount),
    )
    await conn.commit()


async def set_sell_alerts(conn, chat_id: int, on: bool) -> None:
    await conn.execute(
        "INSERT INTO chat_settings (chat_id, watch, sell_alerts) VALUES (?, 0, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET sell_alerts = excluded.sell_alerts",
        (chat_id, 1 if on else 0),
    )
    await conn.commit()


async def set_flag(conn, chat_id: int, column: str, on: bool) -> None:
    """Toggle one boolean chat setting. Column name is whitelisted, not free text."""
    if column not in ("watch", "autoscan", "autoharvest", "sell_alerts"):
        raise ValueError(f"unknown setting {column}")
    await conn.execute(
        f"INSERT INTO chat_settings (chat_id, watch, {column}) VALUES (?, 0, ?)"
        f" ON CONFLICT(chat_id) DO UPDATE SET {column} = excluded.{column}",
        (chat_id, 1 if on else 0),
    )
    await conn.commit()


async def chats_with(conn, column: str) -> list[int]:
    if column not in ("watch", "autoscan", "autoharvest"):
        raise ValueError(f"unknown setting {column}")
    rows = await (await conn.execute(
        f"SELECT chat_id FROM chat_settings WHERE {column} = 1"
    )).fetchall()
    return [r["chat_id"] for r in rows]


# ------------------------------------------------------------------ scan reports
# A repeat scan should only speak when something moved. "Moved" means a token
# reached the overlap bar for the first time, picked up another wallet, or its
# score crossed a half-point band -- not that the clock ticked.
SCORE_STEP = 0.5


async def scan_delta(conn, chat_id: int, chain: str, token: str,
                     wallets: int, score: float) -> str | None:
    """Classify this scan result against what was last reported.

    Returns "new", "stronger", "rescored", or None when there is nothing
    worth repeating. Records the current state either way.
    """
    row = await (await conn.execute(
        "SELECT wallets, score FROM scan_reports"
        " WHERE chat_id = ? AND chain = ? AND token = ?",
        (chat_id, chain, token),
    )).fetchone()

    if row is None:
        verdict = "new"
    elif wallets > row["wallets"]:
        verdict = "stronger"
    elif abs(score - row["score"]) >= SCORE_STEP:
        verdict = "rescored"
    else:
        verdict = None

    if verdict:
        await conn.execute(
            "INSERT INTO scan_reports (chat_id, chain, token, wallets, score, reported_at)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(chat_id, chain, token) DO UPDATE SET"
            "   wallets = excluded.wallets, score = excluded.score,"
            "   reported_at = excluded.reported_at",
            (chat_id, chain, token, wallets, score, int(time.time())),
        )
        await conn.commit()
    return verdict


async def forget_scan_reports(conn, chat_id: int) -> None:
    await conn.execute("DELETE FROM scan_reports WHERE chat_id = ?", (chat_id,))
    await conn.commit()


# ---------------------------------------------------------------------- outcomes
async def record_outcome(conn, chat_id: int, chain: str, wallet: str, token: str,
                         symbol: str, kind: str, score: float | None,
                         entry_price: float) -> int | None:
    """Start tracking what happens after an alert. Unpriced tokens are skipped:
    a grade computed from a zero entry price would be fiction."""
    if entry_price <= 0:
        return None
    now = int(time.time())
    cur = await conn.execute(
        "INSERT INTO alert_outcomes (chat_id, chain, wallet, token, symbol, kind,"
        " score, entry_price, peak_price, last_price, opened_at, last_check)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (chat_id, chain, wallet, token, symbol, kind, score,
         entry_price, entry_price, entry_price, now, now),
    )
    await conn.commit()
    return cur.lastrowid


async def open_outcomes(conn, limit: int = 200) -> list[aiosqlite.Row]:
    return list(await (await conn.execute(
        "SELECT * FROM alert_outcomes WHERE closed = 0 ORDER BY last_check ASC LIMIT ?",
        (limit,),
    )).fetchall())


async def update_outcome(conn, outcome_id: int, last_price: float,
                         peak_price: float, closed: bool) -> None:
    await conn.execute(
        "UPDATE alert_outcomes SET last_price = ?, peak_price = ?, last_check = ?,"
        " closed = ? WHERE id = ?",
        (last_price, peak_price, int(time.time()), 1 if closed else 0, outcome_id),
    )
    await conn.commit()


async def outcomes_for_chat(conn, chat_id: int) -> list[aiosqlite.Row]:
    return list(await (await conn.execute(
        "SELECT * FROM alert_outcomes WHERE chat_id = ? ORDER BY opened_at DESC",
        (chat_id,),
    )).fetchall())


# ----------------------------------------------------------------- harvest queue
async def queue_add(conn, chat_id: int, token: str, symbol: str | None,
                    source: str = "manual") -> bool:
    cur = await conn.execute(
        "INSERT OR IGNORE INTO harvest_queue (chat_id, token, symbol, source, added_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (chat_id, token, symbol, source, int(time.time())),
    )
    await conn.commit()
    return cur.rowcount > 0


async def queue_next(conn, chat_id: int) -> aiosqlite.Row | None:
    return await (await conn.execute(
        "SELECT * FROM harvest_queue WHERE chat_id = ? AND status = 'pending'"
        " ORDER BY added_at ASC LIMIT 1",
        (chat_id,),
    )).fetchone()


async def queue_mark(conn, chat_id: int, token: str, status: str,
                     note: str | None = None) -> None:
    await conn.execute(
        "UPDATE harvest_queue SET status = ?, note = ?, done_at = ?"
        " WHERE chat_id = ? AND token = ?",
        (status, note, int(time.time()), chat_id, token),
    )
    await conn.commit()


async def queue_list(conn, chat_id: int) -> list[aiosqlite.Row]:
    return list(await (await conn.execute(
        "SELECT * FROM harvest_queue WHERE chat_id = ?"
        " ORDER BY (status = 'pending') DESC, added_at ASC",
        (chat_id,),
    )).fetchall())


async def queue_clear(conn, chat_id: int, only_done: bool = True) -> int:
    sql = "DELETE FROM harvest_queue WHERE chat_id = ?"
    if only_done:
        sql += " AND status != 'pending'"
    cur = await conn.execute(sql, (chat_id,))
    await conn.commit()
    return cur.rowcount


async def set_min_score(conn, chat_id: int, score: float) -> None:
    await conn.execute(
        "INSERT INTO chat_settings (chat_id, watch, min_score) VALUES (?, 0, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET min_score = excluded.min_score",
        (chat_id, score),
    )
    await conn.commit()
