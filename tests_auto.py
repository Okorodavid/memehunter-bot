"""Checks for the scheduling, queue and leaderboard layers.

Runs offline with no API keys. Imported by selftest.py, or run directly:
    python tests_auto.py
"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "auto.db"))

from memehunter import db                                    # noqa: E402
from memehunter.pipeline.leaderboard import (build_scorecards,  # noqa: E402
                                             stale_wallets, summarise,
                                             underperformers)
from memehunter.providers.base import (EVM_CHAINS,            # noqa: E402
                                       chain_from_dex_id)
from memehunter.providers.evm import EvmProvider              # noqa: E402

PASSED = FAILED = 0


def _local_check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


# Reassigned by run() so selftest.py can pool these results with its own.
check = _local_check


def outcome(wallet: str, entry: float, peak: float, last: float,
            kind: str = "BUY", symbol: str = "TOK", opened: int | None = None,
            closed: int = 1) -> dict:
    return {
        "wallet": wallet, "chain": "solana", "token": "TOKEN" + symbol,
        "symbol": symbol, "kind": kind, "entry_price": entry,
        "peak_price": peak, "last_price": last, "score": 8.0,
        "opened_at": opened if opened is not None else int(time.time()),
        "closed": closed,
    }


# --------------------------------------------------------------- leaderboard
def test_scorecards() -> None:
    rows = [
        outcome("A", 1.0, 3.0, 2.0, symbol="X"),      # 3x peak  -> hit
        outcome("A", 1.0, 2.0, 0.5, symbol="Y"),      # 2x peak  -> hit
        outcome("A", 1.0, 1.1, 1.0, symbol="Z"),      # miss
        outcome("B", 1.0, 1.0, 0.3, symbol="Q"),      # miss
    ]
    cards = {c.wallet: c for c in build_scorecards(rows)}
    a, b = cards["A"], cards["B"]

    check("counts every graded call", a.signals == 3, f"got {a.signals}")
    check("counts hits at the win bar", a.hits == 2, f"got {a.hits}")
    check("hit rate is hits/signals", abs(a.hit_rate - 2 / 3) < 1e-9)
    check("avg peak averages multiples",
          abs(a.avg_peak - (3 + 2 + 1.1) / 3) < 1e-9, f"got {a.avg_peak}")
    check("median peak is the middle call", abs(a.median_peak - 2.0) < 1e-9)
    check("best peak names its coin", a.best_symbol == "X" and a.best_peak == 3.0)
    check("worst tracks where it stands now", b.worst_symbol == "Q")
    check("better wallet ranks first", build_scorecards(rows)[0].wallet == "A")


def test_small_samples_are_shrunk() -> None:
    lucky = [outcome("LUCKY", 1.0, 5.0, 5.0)]
    steady = [outcome("STEADY", 1.0, 2.0, 2.0, symbol=f"S{i}") for i in range(12)]
    steady += [outcome("STEADY", 1.0, 1.0, 1.0, symbol=f"L{i}") for i in range(8)]

    cards = build_scorecards(lucky + steady)
    check("a 1-for-1 wallet does not top a 12-for-20",
          cards[0].wallet == "STEADY", f"got {cards[0].wallet}")

    one = next(c for c in cards if c.wallet == "LUCKY")
    check("raw hit rate of 1-for-1 is 100%", one.hit_rate == 1.0)
    check("confidence is shrunk well below it", one.confidence < 0.6,
          f"got {one.confidence}")
    check("fewer than 3 calls grades NEW", one.grade == "NEW", one.grade)


def test_round_trip_detection() -> None:
    rows = [outcome("RT", 1.0, 4.0, 1.0, symbol=f"T{i}") for i in range(4)]
    card = build_scorecards(rows)[0]
    check("spots calls that give it all back", card.round_trip)
    check("says so in the verdict", "exit yourself" in card.verdict, card.verdict)

    holds = [outcome("HOLD", 1.0, 4.0, 3.5, symbol=f"H{i}") for i in range(4)]
    check("does not flag one that held its gain",
          not build_scorecards(holds)[0].round_trip)


def test_ignores_ungradeable() -> None:
    rows = [
        outcome("A", 0.0, 0.0, 0.0),                  # never priced
        outcome("A", 1.0, 2.0, 2.0),
        outcome("A", 1.0, 9.0, 9.0, kind="EXIT"),     # exits are not graded
    ]
    card = build_scorecards(rows)[0]
    check("skips unpriced entries", card.signals == 1, f"got {card.signals}")
    check("skips exit alerts", card.best_peak == 2.0, f"got {card.best_peak}")


def test_summary_and_pruning() -> None:
    old = int(time.time()) - 40 * 86400
    rows = [outcome("A", 1.0, 3.0, 3.0), outcome("A", 1.0, 1.0, 1.0)]
    rows += [outcome("DEAD", 1.0, 2.0, 2.0, opened=old)]
    rows += [outcome("BAD", 1.0, 1.0, 0.2, symbol=f"B{i}") for i in range(6)]

    cards = build_scorecards(rows)
    s = summarise(cards)
    check("summary counts all signals", s["signals"] == 9, f"got {s['signals']}")
    check("summary counts all wallets", s["wallets"] == 3)
    check("summary hit rate spans wallets", abs(s["hit_rate"] - 2 / 9) < 1e-9)

    check("finds silent wallets",
          [c.wallet for c in stale_wallets(cards)] == ["DEAD"])
    check("finds losing wallets",
          [c.wallet for c in underperformers(cards)] == ["BAD"])
    check("does not prune a wallet with too few calls",
          "A" not in [c.wallet for c in underperformers(cards)])


def test_empty_board() -> None:
    cards = build_scorecards([])
    s = summarise(cards)
    check("empty board has no signals", s["signals"] == 0 and s["wallets"] == 0)
    check("empty board does not divide by zero", s["hit_rate"] == 0.0)


# ----------------------------------------------------------------- chains
def test_chain_table() -> None:
    for chain, meta in EVM_CHAINS.items():
        check(f"{chain} has a chain id", isinstance(meta.get("chainid"), int))
        check(f"{chain} has an explorer", str(meta.get("explorer", "")).startswith("http"))
        check(f"{chain} declares gas decimals", isinstance(meta.get("decimals"), int))
        check(f"{chain} lists its own key as a dex id", chain in meta["dex_ids"])

    ids = [i for m in EVM_CHAINS.values() for i in m["dex_ids"]]
    check("no dex id is claimed by two chains", len(ids) == len(set(ids)),
          f"dupes: {[i for i in ids if ids.count(i) > 1]}")
    chainids = [m["chainid"] for m in EVM_CHAINS.values()]
    check("no chain id is duplicated", len(chainids) == len(set(chainids)))

    check("robinhood chain is chain id 4663", EVM_CHAINS["robinhood"]["chainid"] == 4663)
    check("arc is chain id 5042", EVM_CHAINS["arc"]["chainid"] == 5042)
    check("arc gas is 6 decimals, not 18", EVM_CHAINS["arc"]["decimals"] == 6)


def test_dex_id_resolution() -> None:
    check("plain name resolves", chain_from_dex_id("base") == "base")
    check("case is ignored", chain_from_dex_id("BSC") == "bsc")
    check("whitespace is ignored", chain_from_dex_id("  arc ") == "arc")
    check("an alias resolves", chain_from_dex_id("binance-smart-chain") == "bsc")
    check("underscores normalise to hyphens",
          chain_from_dex_id("robinhood_chain") == "robinhood")
    check("hyphens can also be dropped",
          chain_from_dex_id("arc-mainnet") == "arc")
    # The important one: a chain we cannot query must NOT come back as ethereum,
    # or we would query a real contract at that address on the wrong chain.
    check("an unknown chain resolves to nothing", chain_from_dex_id("sui") is None)
    check("empty input resolves to nothing", chain_from_dex_id("") is None)


def test_native_decimals() -> None:
    check("an 18-decimal chain keeps 18 decimals", EvmProvider("base").decimals == 18)
    check("arc uses 6", EvmProvider("arc").decimals == 6)
    check("an unknown chain falls back to ethereum",
          EvmProvider("nonsense").chain == "ethereum")
    check("robinhood keeps its own explorer",
          "robinhood" in EvmProvider("robinhood").explorer)


# ------------------------------------------------------- multi-chain tracking
async def test_empty_wallet_first_buy_alerts() -> None:
    """The bug this whole feature depends on not having.

    A wallet holding nothing on a chain still needs a baseline. Deciding that
    from position rows alone meant an empty wallet was re-seeded every poll and
    its first buy there was never reported -- which is the normal case once a
    wallet is watched on eight chains.
    """
    from memehunter.pipeline.events import diff_positions
    conn = await db.connect()
    chain, w = "arc", "0xEMPTY"

    check("an unseen wallet-chain has no baseline",
          not await db.has_snapshot(conn, chain, w))

    # Poll 1: they hold nothing here yet.
    await db.save_snapshot(conn, chain, w, {})
    await db.mark_seeded(conn, chain, w)
    check("an empty wallet still gets a baseline",
          await db.has_snapshot(conn, chain, w))

    # Poll 2: first ever buy on this chain.
    previous = await db.position_snapshot(conn, chain, w)
    events = diff_positions(previous, {"TOKEN": 100.0})
    check("the first buy on an empty wallet-chain fires",
          [e.kind for e in events] == ["BUY"], str([e.kind for e in events]))
    await conn.close()


async def test_dormancy_cycle() -> None:
    conn = await db.connect()
    chat, w = 7100, "0xMULTI"
    await db.add_wallet(conn, chat, "base", w, "good wallet", None)
    for chain in ("arc", "robinhood"):
        await db.add_wallet(conn, chat, chain, w, "good wallet", None, auto=True)

    rows = await db.wallets_to_poll(conn, chat, 43200, 12)
    check("a new wallet is polled on every chain", len(rows) == 3, f"got {len(rows)}")

    # Two empty reads must NOT park it: Etherscan returns empty for failures too.
    for _ in range(2):
        await db.record_probe(conn, chat, "arc", w, False, dormant_after=3)
    rows = await db.wallets_to_poll(conn, chat, 43200, 12)
    check("two empty reads do not park a chain", len(rows) == 3, f"got {len(rows)}")

    parked = await db.record_probe(conn, chat, "arc", w, False, dormant_after=3)
    check("the third empty read parks it", parked)
    rows = await db.wallets_to_poll(conn, chat, 43200, 12)
    check("a parked chain stops being polled", len(rows) == 2, f"got {len(rows)}")

    # ...but it must come back on the retry schedule, or a wallet that starts
    # using a new chain next month is never noticed. Backdate the probe to
    # simulate the 12h window elapsing, rather than asking for a zero window.
    await conn.execute(
        "UPDATE tracked_wallets SET last_probe = ? WHERE chat_id = ? AND chain = ?",
        (int(time.time()) - 13 * 3600, chat, "arc"))
    await conn.commit()

    check("a parked chain is not retried before it is due",
          len(await db.wallets_to_poll(conn, chat, 24 * 3600, 12)) == 2)
    rows = await db.wallets_to_poll(conn, chat, 12 * 3600, 12)
    check("a parked chain is retried once due", len(rows) == 3, f"got {len(rows)}")
    check("retries are budget capped",
          len(await db.wallets_to_poll(conn, chat, 12 * 3600, 0)) == 2)

    # Activity on the parked chain wakes it immediately.
    await db.record_probe(conn, chat, "arc", w, True, dormant_after=3)
    rows = await db.wallets_to_poll(conn, chat, 43200, 0)
    check("activity revives a parked chain", len(rows) == 3, f"got {len(rows)}")

    chains = await db.wallet_chains(conn, chat, w)
    check("one wallet spans several chain rows", len(chains) == 3)
    check("the chain it was added on is not marked auto",
          [c["chain"] for c in chains if not c["auto"]] == ["base"])
    check("untrack removes every chain for that wallet",
          await db.remove_wallet(conn, chat, w) == 3)
    await conn.close()


async def test_leaderboard_pools_across_chains() -> None:
    """A wallet's record must be one record, not one per chain -- the whole
    point of following it onto a new chain is the record it already has."""
    rows = [
        {**outcome("0xW", 1.0, 3.0, 2.5, symbol="A"), "chain": "base"},
        {**outcome("0xW", 1.0, 2.0, 1.8, symbol="B"), "chain": "base"},
        {**outcome("0xW", 1.0, 4.0, 3.0, symbol="C"), "chain": "arc"},
    ]
    cards = build_scorecards(rows)
    check("one card per wallet, not per chain", len(cards) == 1, f"got {len(cards)}")
    check("calls on both chains count", cards[0].signals == 3, f"got {cards[0].signals}")
    check("a cross-chain wallet can be graded", cards[0].grade in ("A", "B", "C", "D"))


# ------------------------------------------------------------------ database
async def test_scan_delta() -> None:
    conn = await db.connect()
    chat = 5150

    first = await db.scan_delta(conn, chat, "solana", "TOK", 3, 8.0)
    check("first sighting reports as new", first == "new", str(first))

    same = await db.scan_delta(conn, chat, "solana", "TOK", 3, 8.0)
    check("an unchanged repeat stays silent", same is None, str(same))

    tiny = await db.scan_delta(conn, chat, "solana", "TOK", 3, 8.2)
    check("a trivial score move stays silent", tiny is None, str(tiny))

    more = await db.scan_delta(conn, chat, "solana", "TOK", 4, 8.0)
    check("one more wallet is worth repeating", more == "stronger", str(more))

    rescored = await db.scan_delta(conn, chat, "solana", "TOK", 4, 9.0)
    check("a real score move is worth repeating", rescored == "rescored",
          str(rescored))

    check("a drop back to fewer wallets stays silent",
          await db.scan_delta(conn, chat, "solana", "TOK", 3, 9.0) is None)
    check("another chat is tracked separately",
          await db.scan_delta(conn, 9999, "solana", "TOK", 3, 8.0) == "new")
    await conn.close()


async def test_outcomes_roundtrip() -> None:
    conn = await db.connect()
    chat = 5151

    oid = await db.record_outcome(conn, chat, "solana", "W1", "TOK", "TOK",
                                  "BUY", 8.0, 0.001)
    check("records a priced alert", oid is not None)
    skipped = await db.record_outcome(conn, chat, "solana", "W1", "NOPRICE",
                                      "NP", "BUY", 8.0, 0.0)
    check("refuses to grade an unpriced alert", skipped is None)

    rows = await db.open_outcomes(conn)
    check("open outcomes come back", len(rows) == 1, f"got {len(rows)}")
    check("entry price is stored", abs(rows[0]["entry_price"] - 0.001) < 1e-12)

    await db.update_outcome(conn, oid, 0.003, 0.004, closed=False)
    row = (await db.open_outcomes(conn))[0]
    check("peak is carried, not overwritten", abs(row["peak_price"] - 0.004) < 1e-12)
    check("last price updates", abs(row["last_price"] - 0.003) < 1e-12)

    await db.update_outcome(conn, oid, 0.002, 0.004, closed=True)
    check("closing removes it from the open set", not await db.open_outcomes(conn))
    # The unpriced alert was never stored, so history holds exactly the one
    # gradeable call -- an ungradeable row would poison every average.
    check("but it stays on the record",
          len(await db.outcomes_for_chat(conn, chat)) == 1)
    await conn.close()


async def test_queue() -> None:
    conn = await db.connect()
    chat = 5152

    check("adds to the queue", await db.queue_add(conn, chat, "AAA", "A", "manual"))
    check("does not add the same coin twice",
          not await db.queue_add(conn, chat, "AAA", "A", "manual"))
    await db.queue_add(conn, chat, "BBB", "B", "manual")

    nxt = await db.queue_next(conn, chat)
    check("serves the queue oldest first", nxt["token"] == "AAA", nxt["token"])

    await db.queue_mark(conn, chat, "AAA", "done", "7 survivors")
    nxt = await db.queue_next(conn, chat)
    check("a finished coin is not served again", nxt["token"] == "BBB", nxt["token"])

    await db.queue_mark(conn, chat, "BBB", "failed", "history too deep")
    check("an empty queue returns nothing", await db.queue_next(conn, chat) is None)

    rows = await db.queue_list(conn, chat)
    check("history is kept for review", len(rows) == 2)
    removed = await db.queue_clear(conn, chat, only_done=True)
    check("clear removes only finished entries", removed == 2, f"got {removed}")
    await conn.close()


async def test_flags_and_migration() -> None:
    conn = await db.connect()
    chat = 5153

    check("autoscan is off by default",
          chat not in await db.chats_with(conn, "autoscan"))
    await db.set_flag(conn, chat, "autoscan", True)
    check("autoscan turns on", chat in await db.chats_with(conn, "autoscan"))
    check("autoharvest is independent of it",
          chat not in await db.chats_with(conn, "autoharvest"))

    await db.set_flag(conn, chat, "autoharvest", True)
    await db.set_flag(conn, chat, "autoscan", False)
    check("flags toggle independently",
          chat not in await db.chats_with(conn, "autoscan")
          and chat in await db.chats_with(conn, "autoharvest"))

    # Turning a flag on must not disturb settings set earlier.
    await db.set_bankroll(conn, chat, 2500)
    await db.set_flag(conn, chat, "autoscan", True)
    settings = await db.get_settings(conn, chat)
    check("toggling a flag preserves bankroll", settings["bankroll"] == 2500,
          str(settings["bankroll"]))
    check("and preserves the other flag", settings["autoharvest"] == 1)

    bad = False
    try:
        await db.chats_with(conn, "watch; DROP TABLE chat_settings")
    except ValueError:
        bad = True
    check("rejects an unknown settings column", bad)
    await conn.close()


async def test_migrates_old_database() -> None:
    """A database from the previous version must upgrade without losing rows."""
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE tracked_wallets (
            chat_id INTEGER NOT NULL, chain TEXT NOT NULL, address TEXT NOT NULL,
            label TEXT, source_token TEXT, added_at INTEGER NOT NULL,
            PRIMARY KEY (chat_id, chain, address));
        CREATE TABLE chat_settings (
            chat_id INTEGER PRIMARY KEY, watch INTEGER NOT NULL DEFAULT 0,
            min_score REAL, bankroll REAL,
            sell_alerts INTEGER NOT NULL DEFAULT 1);
        INSERT INTO tracked_wallets VALUES (7, 'solana', 'WALLET1', 'BONK #1', 'TOK', 1);
        INSERT INTO chat_settings VALUES (7, 1, 6.5, 1000, 1);
    """)
    old.commit()
    old.close()

    original = os.environ.get("DB_PATH")
    from memehunter.config import CFG
    object.__setattr__(CFG, "db_path", path)
    try:
        conn = await db.connect()
        rows = await db.list_wallets(conn, 7)
        check("migration keeps tracked wallets", len(rows) == 1)
        settings = await db.get_settings(conn, 7)
        check("migration keeps settings", settings["bankroll"] == 1000)
        check("migration adds autoscan off", settings["autoscan"] == 0)
        check("migration adds autoharvest off", settings["autoharvest"] == 0)
        check("new tables exist after migration",
              await db.queue_add(conn, 7, "NEW", None, "manual"))
        await conn.close()
    finally:
        object.__setattr__(CFG, "db_path", original or "memehunter.db")


async def run(check_fn=None) -> tuple[int, int]:
    """Run every check. selftest.py passes its own `check` so the totals pool."""
    global check
    check = check_fn or _local_check

    print("\nchains")
    test_chain_table()
    test_dex_id_resolution()
    test_native_decimals()

    print("\nleaderboard maths")
    test_scorecards()
    test_small_samples_are_shrunk()
    test_round_trip_detection()
    test_ignores_ungradeable()
    test_summary_and_pruning()
    test_empty_board()

    print("\nmulti-chain tracking")
    await test_empty_wallet_first_buy_alerts()
    await test_dormancy_cycle()
    await test_leaderboard_pools_across_chains()

    print("\nscheduling state")
    await test_scan_delta()
    await test_outcomes_roundtrip()
    await test_queue()
    await test_flags_and_migration()
    await test_migrates_old_database()
    return PASSED, FAILED


if __name__ == "__main__":
    p, f = asyncio.run(run())
    print(f"\n{p} passed, {f} failed")
    raise SystemExit(1 if f else 0)
