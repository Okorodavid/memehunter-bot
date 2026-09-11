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
    PRIMARY KEY (chat_id, chain, address)
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
    sell_alerts INTEGER NOT NULL DEFAULT 1
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
                     label: str | None, source_token: str | None) -> bool:
    """Returns True when the wallet was newly added, False when already tracked."""
    cur = await conn.execute(
        "INSERT OR IGNORE INTO tracked_wallets (chat_id, chain, address, label, source_token, added_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, chain, address, label, source_token, int(time.time())),
    )
    await conn.commit()
    return cur.rowcount > 0


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
    row = await (await conn.execute(
        "SELECT 1 FROM wallet_positions WHERE chain = ? AND wallet = ? LIMIT 1",
        (chain, wallet),
    )).fetchone()
    return row is not None


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


async def set_min_score(conn, chat_id: int, score: float) -> None:
    await conn.execute(
        "INSERT INTO chat_settings (chat_id, watch, min_score) VALUES (?, 0, ?)"
        " ON CONFLICT(chat_id) DO UPDATE SET min_score = excluded.min_score",
        (chat_id, score),
    )
    await conn.commit()
