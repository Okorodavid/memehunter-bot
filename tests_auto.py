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


# ------------------------------------------------------------ progress safety
async def test_progress_never_aborts_the_job() -> None:
    """The status line is cosmetic. A Telegram timeout updating it used to
    escape and kill the scan underneath, throwing away a minute of real work
    because a message could not be redrawn."""
    from telegram.error import BadRequest, NetworkError, TelegramError, TimedOut
    from memehunter.bot import Progress

    class Msg:
        def __init__(self, exc): self.exc = exc
        async def edit_text(self, text):
            if self.exc:
                raise self.exc

    for exc in (TimedOut(), NetworkError("dropped"), BadRequest("not modified"),
                TelegramError("something"), RuntimeError("unexpected")):
        raised = None
        try:
            await Progress(Msg(exc))("step 2 of 7")
        except Exception as caught:      # noqa: BLE001 - that is the point
            raised = caught
        check(f"{type(exc).__name__} does not abort the job", raised is None,
              f"escaped: {raised!r}")

    # And it must still actually send when nothing is wrong.
    sent = []

    class Ok:
        async def edit_text(self, text): sent.append(text)

    p = Progress(Ok())
    await p("first")
    await p("first")          # identical text is skipped
    await p("second")
    check("identical updates are not re-sent", sent == ["first", "second"], str(sent))


# ------------------------------------------------------- provider error paths
async def test_etherscan_errors_are_not_empty_results() -> None:
    """Etherscan answers a rejected key and an empty result the same way.
    Collapsing both into [] made every EVM failure read as "no early buyers",
    which is a statement about the coin rather than about the broken key."""
    import os as _os
    from memehunter.providers import evm as E

    class Http:
        def __init__(self, payload): self.payload = payload
        async def get(self, url, params=None): return self.payload
        async def aclose(self): pass

    async def call(payload):
        p = E.EvmProvider("base")
        p.http, p.last_error = Http(payload), ""
        rows = await p._call("account", "tokentx", contractaddress="0xdead")
        return rows, p.last_error

    rows, err = await call({"status": "1", "message": "OK",
                            "result": [{"to": "0xa", "timeStamp": "1"}]})
    check("a good response returns rows", len(rows) == 1 and err == "")

    rows, err = await call({"status": "0", "message": "No transactions found",
                            "result": []})
    check("a genuine empty is not an error", rows == [] and err == "")

    for detail in ("Invalid API Key", "Max rate limit reached",
                   "Invalid chainid, please check"):
        rows, err = await call({"status": "0", "message": "NOTOK", "result": detail})
        check(f"'{detail[:18]}' is reported, not swallowed", err == detail, err)

    rows, err = await call({"status": "0", "message": "NOTOK", "result": ""})
    check("an error with no detail still reports something", bool(err))

    check("base is chainid 8453 on the probe", E.EvmProvider("base").chainid == 8453)


def test_diagnostics_verdict() -> None:
    from memehunter.diagnostics import Check, verdict

    ok = [Check("DexScreener", True, ""), Check("Helius RPC", True, "")]
    check("all-clear says so", "working" in verdict(ok))

    core_down = ok[:1] + [Check("Helius RPC", False, "timeout")]
    check("a core failure is called out as fatal", "not" in verdict(core_down)
          and "optional" in verdict(core_down))

    chain_down = ok + [Check("Etherscan / base", False, "Invalid API Key")]
    v = verdict(chain_down)
    check("a chain failure names the chain", "Etherscan / base" in v)
    check("and says emptiness is the failure, not a verdict", "not a verdict" in v)

    skipped = ok + [Check("Solscan", False, "no key", skipped=True)]
    check("a skipped optional source is not a failure", "working" in verdict(skipped))


# ------------------------------------------------------------------- backups
async def test_backup_survives_a_wipe() -> None:
    """A free host rebuilds the filesystem on every redeploy. The backup is the
    only thing standing between that and a bot that silently knows nothing."""
    import gzip, shutil, tempfile as tf
    from pathlib import Path
    from memehunter import backup as bk
    from memehunter.config import CFG

    class Doc:
        def __init__(self, path, name):
            self.file_id, self.file_name = str(path), name
            self.file_size = Path(path).stat().st_size

    class Handle:
        def __init__(self, src): self.src = src
        async def download_to_drive(self, custom_path):
            shutil.copyfile(self.src, custom_path)

    class Msg:
        def __init__(self, doc=None): self.document, self.message_id = doc, 1

    class Bot:
        def __init__(self): self.pinned = self.last = None
        async def send_document(self, chat_id, document, filename, caption, **kw):
            keep = Path(tf.mkdtemp()) / filename
            keep.write_bytes(document.read())
            self.last = Doc(keep, filename)
            return Msg(self.last)
        async def unpin_all_chat_messages(self, chat_id): self.pinned = None
        async def pin_chat_message(self, chat_id, mid, **kw): self.pinned = Msg(self.last)
        async def get_chat(self, chat_id):
            return type("Chat", (), {"pinned_message": self.pinned})()
        async def get_file(self, fid): return Handle(fid)

    original = CFG.db_path
    object.__setattr__(CFG, "db_path", os.path.join(tempfile.mkdtemp(), "bk.db"))
    try:
        conn = await db.connect()
        check("a fresh database reports empty", await bk.is_empty(conn))

        await db.add_wallet(conn, 900, "solana", "W_A", "whale", "TOK")
        await db.add_wallet(conn, 900, "base", "0xBEEF", "evm", None)
        await db.record_outcome(conn, 900, "solana", "W_A", "TOK", "TOK",
                                "BUY", 8.0, 0.01)
        await db.save_snapshot(conn, "solana", "W_A", {"MINT1": 100.0})
        check("a populated database does not report empty", not await bk.is_empty(conn))

        bot = Bot()
        check("backup sends and pins", await bk.send(bot, 900, conn))
        await conn.close()

        os.remove(CFG.db_path)                      # the redeploy
        conn = await db.connect()
        check("the wiped database is empty", await bk.is_empty(conn))
        await conn.close()

        result = await bk.restore(bot, 900)
        check("restore reports what it recovered", "Restored 2" in result, result)

        conn = await db.connect()
        check("wallets come back", len(await db.list_wallets(conn, 900)) == 2)
        check("alert history comes back",
              len(await db.outcomes_for_chat(conn, 900)) == 1)
        # The subtle one: without baselines the watcher treats every wallet as
        # newly seen and swallows its next buy.
        check("position baselines come back",
              (await db.position_snapshot(conn, "solana", "W_A"))
              .get("MINT1", {}).get("amount") == 100.0)
        check("the seeded marker comes back",
              await db.has_snapshot(conn, "solana", "W_A"))
        await conn.close()

        check("restoring from a non-backup message is refused",
              "no memehunter backup" in await bk.restore(bot, 900, Msg(None)))
        bot.pinned = None
        check("a missing pinned backup is reported, not crashed",
              "No pinned backup" in await bk.restore(bot, 900))
    finally:
        object.__setattr__(CFG, "db_path", original)


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


# ------------------------------------------------------------ name resolution
def test_dns_override() -> None:
    """A poisoned name must be fixed for every caller, including the ones that
    pass the host as bytes -- httpcore does, which is how every Telegram
    request kept reaching the wrong address while direct calls looked fixed."""
    import socket as S

    from memehunter import dns as D

    POISONED, REAL = "108.160.166.137", "149.154.166.110"
    system = {
        "api.telegram.org": [POISONED],
        "blackholed.example": ["0.0.0.0"],
        "normal.example": ["93.184.216.34"],
        "gone.example": None,                      # resolver says it does not exist
    }

    def fake_system(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if name == "8.8.8.8":
            return [(S.AF_INET, S.SOCK_STREAM, 0, "", (name, port))]
        addresses = system.get(name)
        if addresses is None:
            raise S.gaierror(11001, "getaddrinfo failed")
        return [(S.AF_INET, S.SOCK_STREAM, 0, "", (a, port)) for a in addresses]

    asked = []

    # The public resolver knows these two and, like the real one, cannot
    # invent an answer for a name that does not exist.
    known = {"api.telegram.org", "blackholed.example"}

    def fake_query(host, resolvers, timeout=8.0):
        asked.append(host)
        return ([REAL], 300.0) if host in known else ([], 60.0)

    original_real, original_query = D._real_getaddrinfo, D.query
    D._real_getaddrinfo, D.query = fake_system, fake_query
    try:
        D.install(["api.telegram.org"], ("https://8.8.8.8/resolve",))

        got = S.getaddrinfo("api.telegram.org", 443)
        check("a poisoned name is corrected", got[0][4][0] == REAL, str(got))

        D._cache.clear()
        got = S.getaddrinfo(b"api.telegram.org", 443)
        check("a bytes host is corrected too", got[0][4][0] == REAL, str(got))

        D._cache.clear()
        got = S.getaddrinfo("api.telegram.org", "https")
        check("a service name still yields port 443", got[0][4][1] == 443, str(got))

        before = len(asked)
        got = S.getaddrinfo("normal.example", 443)
        check("an ordinary name is left to the system resolver",
              got[0][4][0] == "93.184.216.34" and len(asked) == before, str(got))

        got = S.getaddrinfo("blackholed.example", 443)
        check("a blackholed answer falls through to HTTPS", got[0][4][0] == REAL, str(got))

        try:
            S.getaddrinfo("gone.example", 443)
            raised = False
        except S.gaierror:
            raised = True
        check("a name that truly does not resolve still raises", raised)

        before = len(asked)
        got = S.getaddrinfo("8.8.8.8", 443)
        check("an IP address is never sent to a resolver",
              got[0][4][0] == "8.8.8.8" and len(asked) == before, str(got))

        D._cache.clear()
        S.getaddrinfo("api.telegram.org", 443)
        count = len(asked)
        S.getaddrinfo("api.telegram.org", 443)
        check("the answer is cached, not re-asked", len(asked) == count)
    finally:
        D.uninstall()
        D._real_getaddrinfo, D.query = original_real, original_query


def test_dns_parses_a_doh_answer() -> None:
    """The resolver reads real DNS-over-HTTPS JSON, and refuses a blackholed
    answer rather than passing 0.0.0.0 off as an address."""
    from memehunter import dns as D

    bodies = {
        "good": {"Status": 0, "Answer": [{"type": 5, "data": "cname.example"},
                                         {"type": 1, "TTL": 300,
                                          "data": "149.154.166.110"}]},
        "blackhole": {"Status": 0, "Answer": [{"type": 1, "TTL": 300,
                                               "data": "0.0.0.0"}]},
        "nxdomain": {"Status": 3, "Answer": []},
    }
    payload = {"body": bodies["good"]}

    class Response:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            return Response(payload["body"])

    original = D.httpx.Client
    D.httpx.Client = lambda *a, **kw: Client()
    try:
        addresses, ttl = D.query("api.telegram.org", ("https://8.8.8.8/resolve",))
        check("an A record is read, a CNAME ignored",
              addresses == ["149.154.166.110"], str(addresses))
        check("the record's TTL is honoured", ttl == 300.0, str(ttl))

        payload["body"] = bodies["blackhole"]
        addresses, _ = D.query("api.telegram.org", ("https://8.8.8.8/resolve",))
        check("0.0.0.0 is not treated as an address", addresses == [], str(addresses))

        payload["body"] = bodies["nxdomain"]
        addresses, _ = D.query("api.telegram.org", ("https://8.8.8.8/resolve",))
        check("a non-zero status is not an answer", addresses == [], str(addresses))
    finally:
        D.httpx.Client = original


async def test_untrack_forgets_the_wallet() -> None:
    """Untracking must remove every chain row and the state behind them.

    Leaving wallet_positions in place meant a re-tracked wallet was compared
    against balances from whenever it was dropped, and announced exits for
    sales that happened while nobody was watching.
    """
    from memehunter.config import CFG

    original = CFG.db_path
    path = os.path.join(tempfile.mkdtemp(), "untrack.db")
    object.__setattr__(CFG, "db_path", path)
    try:
        conn = await db.connect()
        WALLET, OTHER = "0xAAbb00000000000000000000000000000000cCdD", "0xff11"
        for chain in ("base", "ethereum", "bsc"):
            await db.add_wallet(conn, 1, chain, WALLET, "whale", None, auto=chain != "base")
        await db.add_wallet(conn, 2, "base", WALLET, None, None)
        await db.add_wallet(conn, 1, "base", OTHER, None, None)
        await db.record_positions(conn, "base", WALLET, ["0xtok"])
        await db.record_positions(conn, "base", OTHER, ["0xtok"])
        await db.should_alert(conn, 1, "base", WALLET, "0xtok", "BUY")

        async def rows(sql, args):
            return (await (await conn.execute(sql, args)).fetchone())[0]

        removed = await db.remove_wallet(conn, 1, WALLET.lower())
        check("every chain row goes, whatever the case", removed == 3, str(removed))
        check("the wallet is no longer tracked for that chat",
              await rows("SELECT count(*) FROM tracked_wallets"
                         " WHERE chat_id = 1 AND lower(address) = lower(?)",
                         (WALLET,)) == 0)
        check("another chat's row is untouched",
              await rows("SELECT count(*) FROM tracked_wallets WHERE chat_id = 2", ()) == 1)
        check("its alert record is cleared so it can alert again if re-added",
              await rows("SELECT count(*) FROM alerts_sent WHERE chat_id = 1", ()) == 0)
        check("positions stay while another chat still tracks it",
              await rows("SELECT count(*) FROM wallet_positions"
                         " WHERE lower(wallet) = lower(?)", (WALLET,)) == 1)

        await db.remove_wallet(conn, 2, WALLET)
        check("positions go once nobody tracks it",
              await rows("SELECT count(*) FROM wallet_positions"
                         " WHERE lower(wallet) = lower(?)", (WALLET,)) == 0)
        check("another wallet's positions are untouched",
              await rows("SELECT count(*) FROM wallet_positions"
                         " WHERE lower(wallet) = lower(?)", (OTHER,)) == 1)

        check("removing what was never tracked reports nothing",
              await db.remove_wallet(conn, 1, WALLET) == 0)
        await conn.close()
    finally:
        object.__setattr__(CFG, "db_path", original or "memehunter.db")


def test_untrack_accepts_a_pasted_address() -> None:
    """Copying the usage line gives /untrack <0x...>. Taking the first word
    verbatim made that match nothing and report the wallet as untracked while
    it carried on being watched."""
    from memehunter.providers.base import extract_addresses

    ADDR = "0x09ad820aac5779683b481c4674208a4e1b024afa"
    for label, typed in (
        ("a bare address", ADDR),
        ("angle brackets from the usage line", "<" + ADDR + ">"),
        ("an explorer link", "https://basescan.org/address/" + ADDR),
        ("trailing punctuation", ADDR + ","),
    ):
        found = extract_addresses(typed)
        check(label + " is understood", found == [ADDR], str(found))

    two = extract_addresses(ADDR + " 0x" + "b" * 40)
    check("several at once are all picked up", len(two) == 2, str(two))
    check("a label is not mistaken for an address",
          extract_addresses("early BONK whale") == [])


def test_one_instance_per_machine() -> None:
    """A second copy must refuse to start. Two pollers split the update stream
    between them, so commands reach one process or the other at random."""
    import main

    check("the first copy claims the machine", main.claim_this_machine(47699))
    check("a second copy is refused", not main.claim_this_machine(47699))

    held = main._instance_lock
    held.close()
    main._instance_lock = None
    check("the claim is released when the process lets go",
          main.claim_this_machine(47699))
    main._instance_lock.close()
    main._instance_lock = None


# --------------------------------------------------------- long messages
def test_long_messages_stay_valid_html() -> None:
    """Telegram rejects a message whose tags are left open at the cut, and the
    command then sends nothing at all. That is how /tracked went silent once
    the wallet list passed the size limit:

        BadRequest: Can't parse entities: unclosed start tag at byte offset 3804
    """
    from memehunter import render

    def balanced(text):
        stack = []
        for match in render.TAG.finditer(text):
            name = match.group(2).lower()
            if match.group(1):
                if not stack or stack.pop() != name:
                    return False
            else:
                stack.append(name)
        return not stack

    def whole_tags(text):
        opened = text.rfind("<")
        return opened == -1 or text.find(">", opened) != -1

    wallet = ('- <a href="https://basescan.org/address/0x1234">name</a> '
              '<code>0x12..34</code> via 0xab..cd')
    listing = "\n".join([wallet] * 60)
    check("the sample is long enough to matter", len(listing) > render.MAX_MSG)

    one = render.clip(listing)
    check("a clipped message fits", len(one) <= render.MAX_MSG, str(len(one)))
    check("a clipped message closes every tag", balanced(one))
    check("a clipped message never ends mid-tag", whole_tags(one))
    check("a clipped message says it was cut", one.endswith("...(truncated)"))

    parts = render.pages(listing)
    check("a long listing becomes several messages", len(parts) > 1, str(len(parts)))
    check("every page fits", all(len(x) <= render.MAX_MSG for x in parts))
    check("every page is valid HTML", all(balanced(x) for x in parts))
    check("no page ends mid-tag", all(whole_tags(x) for x in parts))
    check("paging loses no wallet", sum(x.count("<a href") for x in parts) == 60)

    short_text = "<b>hi</b>"
    check("short text is one page", render.pages(short_text) == [short_text])
    check("short text is not clipped", render.clip(short_text) == short_text)

    # A cut that lands inside a tag is the exact case Telegram rejected.
    mid = render.clip("<b>" + "x" * render.MAX_MSG + "</b>", 40)
    check("a cut inside a run of text still closes its tag", balanced(mid), mid[-30:])

    giant = "<i>" + "y" * (render.MAX_MSG * 2) + "</i>"
    only = render.pages(giant)
    check("one unsplittable line is still sent", len(only) == 1 and len(only[0]) <= render.MAX_MSG)
    check("that line is valid HTML too", balanced(only[0]))


# ------------------------------------------------------------- top traders
def _sol_tx(owner, mint, token_delta, sol_delta, err=None):
    """One Solana transaction as the RPC returns it, for one owner."""
    pre_token = [{"owner": owner, "mint": mint,
                  "uiTokenAmount": {"uiAmount": max(-token_delta, 0)}}]
    post_token = [{"owner": owner, "mint": mint,
                   "uiTokenAmount": {"uiAmount": max(token_delta, 0)}}]
    return {
        "meta": {
            "err": err,
            "preTokenBalances": pre_token, "postTokenBalances": post_token,
            "preBalances": [10 * 10 ** 9],
            "postBalances": [int((10 + sol_delta) * 10 ** 9)],
        },
        "transaction": {"message": {"accountKeys": [{"pubkey": owner}]},
                        # Unique: the scan reads both ends of a coin's history
                        # and drops what it has already seen by signature.
                        "signatures": [f"sig-{owner}-{token_delta}-{sol_delta}"]},
        "blockTime": 1,
    }


def test_trader_arithmetic() -> None:
    """A trader's multiple is the quote asset out over the quote asset in.

    It is computed before any dollar conversion on purpose: pricing a trade
    from months ago at today's SOL price would put that error into the one
    number the ranking depends on.
    """
    from memehunter.pipeline.traders import Trader

    t = Trader(wallet="W", chain="solana", spent_quote=0.1, received_quote=6.0,
               bought_tokens=1000, sold_tokens=1000, buy_txns=1, sell_txns=2,
               quote_symbol="SOL", quote_price_usd=200.0)
    check("the multiple is quote out over quote in", t.multiple == 60.0, str(t.multiple))
    check("dollars in are priced from the quote", t.spent_usd == 20.0)
    check("dollars out are priced from the quote", t.received_usd == 1200.0)
    check("profit is what came out less what went in", t.pnl_usd == 1180.0)
    check("a wallet that sold everything is not holding", not t.holding)

    half = Trader(wallet="W", chain="solana", spent_quote=1.0, received_quote=2.0,
                  bought_tokens=1000, sold_tokens=400)
    check("a wallet that kept most of it is holding", half.holding)
    check("held tokens are what is left", half.held_tokens == 600)

    never_sold = Trader(wallet="W", chain="solana", spent_quote=1.0)
    check("no sale means no multiple", never_sold.multiple == 0.0)

    free = Trader(wallet="W", chain="solana", received_quote=5.0)
    check("an airdrop cannot divide by zero", free.multiple == 0.0)


def test_trader_ranking() -> None:
    """Rank on the multiple, not the profit, and ignore dust entries."""
    from memehunter.pipeline.traders import Trader, rank

    def trader(name, spent, received, price=200.0):
        return Trader(wallet=name, chain="solana", spent_quote=spent,
                      received_quote=received, quote_price_usd=price)

    small_and_right = trader("SMALL", 0.05, 5.0)       # 100x from $10
    big_and_ok = trader("BIG", 50.0, 150.0)            # 3x from $10,000
    dust = trader("DUST", 0.0000001, 0.01)             # huge multiple, no risk
    loser = trader("LOSER", 1.0, 0.2)
    never_sold = trader("BAGHOLDER", 1.0, 0.0)

    best = rank([big_and_ok, small_and_right, dust, loser, never_sold], 5, 1.0)
    names = [t.wallet for t in best]
    check("the small winner outranks the big one", names[0] == "SMALL", str(names))
    check("a dust entry is excluded", "DUST" not in names, str(names))
    check("a wallet that never sold is excluded", "BAGHOLDER" not in names, str(names))
    check("a loser still appears, ranked last", names[-1] == "LOSER", str(names))
    check("the limit is honoured", len(rank([big_and_ok, small_and_right], 1, 1.0)) == 1)


def test_solana_trade_reading() -> None:
    """Buys and sells come out of per-owner balance deltas.

    A bonding-curve buy pays in bare SOL, which is not a token balance at all.
    Reading only token balances made every pump.fun entry look free, and a
    free entry divides into an infinite multiple.
    """
    from memehunter.pipeline.traders import NATIVE, _owner_deltas, _quote_delta

    MINT = "MintAddress"
    buy = _sol_tx("WALLET", MINT, token_delta=1000.0, sol_delta=-2.0)
    moves = _owner_deltas(buy)["WALLET"]
    check("the token gained is read", moves[MINT] == 1000.0, str(moves))
    check("bare SOL spent is read", round(moves[NATIVE], 6) == -2.0, str(moves))

    quote, stable = _quote_delta(moves)
    check("the quote delta is negative on a buy", round(quote, 6) == -2.0, str(quote))
    check("no stablecoin moved", stable == 0.0)

    sell = _sol_tx("WALLET", MINT, token_delta=-1000.0, sol_delta=6.0)
    moves = _owner_deltas(sell)["WALLET"]
    check("the token sold is read", moves[MINT] == -1000.0, str(moves))
    check("SOL received is read", round(moves[NATIVE], 6) == 6.0, str(moves))


async def test_solana_trader_scan() -> None:
    """The whole Solana path, over a stubbed history."""
    from memehunter.pipeline import traders as T

    MINT = "MintAddress"

    class FakeSol:
        def __init__(self):
            self.orders = []

        async def rpc(self, method, params):
            self.orders.append(params[1]["sortOrder"])
            if params[1]["sortOrder"] == "asc":
                return {"data": [_sol_tx("HERO", MINT, 1000.0, -0.1),
                                 _sol_tx("POOL", MINT, -1000.0, 0.1),
                                 _sol_tx("FAILED", MINT, 500.0, -9.0, err={"x": 1})]}
            return {"data": [_sol_tx("HERO", MINT, -1000.0, 6.0)]}

        async def _only_wallets(self, addresses):
            return {a for a in addresses if a != "POOL"}       # POOL is a program

    class FakeDex:
        async def markets(self, addresses):
            from memehunter.providers.base import TokenMarket
            return {T.WSOL: TokenMarket(address=T.WSOL, chain="solana",
                                        symbol="SOL", price_usd=200.0)}

    class FakeAnalyzer:
        sol = FakeSol()
        dex = FakeDex()

    analyzer = FakeAnalyzer()
    report = await T.top_traders(analyzer, MINT, "solana")

    check("both ends of the history are read",
          set(analyzer.sol.orders) == {"asc", "desc"}, str(analyzer.sol.orders))
    check("a pool is not counted as a trader",
          all(t.wallet != "POOL" for t in report.traders),
          str([t.wallet for t in report.traders]))

    hero = next((t for t in report.traders if t.wallet == "HERO"), None)
    check("the trader is found", hero is not None)
    check("the buy is counted once", hero.buy_txns == 1, str(hero.buy_txns))
    check("the sell is counted once", hero.sell_txns == 1, str(hero.sell_txns))
    check("SOL spent is summed", round(hero.spent_quote, 6) == 0.1, str(hero.spent_quote))
    check("SOL received is summed", round(hero.received_quote, 6) == 6.0)
    check("the multiple follows", round(hero.multiple) == 60, str(hero.multiple))
    check("a failed transaction is ignored",
          all(t.wallet != "FAILED" for t in report.traders))


# ------------------------------------------------------------ command menu
def test_command_menu() -> None:
    """Typing "/" in Telegram lists what the bot can do, and the list has to
    match the handlers. A menu entry with no handler is a command that does
    nothing when tapped, which is worse than not offering it."""
    import re

    from telegram.ext import CommandHandler

    from memehunter.config import CFG

    original = CFG.telegram_token
    object.__setattr__(CFG, "telegram_token", "123456:AAdummy-token-for-tests")
    try:
        from memehunter.bot import COMMAND_MENU, build_app

        names = [name for name, _ in COMMAND_MENU]
        check("the menu is within Telegram's limit of 100", len(names) <= 100,
              str(len(names)))
        check("every name is a legal command",
              all(re.fullmatch(r"[a-z0-9_]{1,32}", n) for n in names),
              str([n for n in names if not re.fullmatch(r"[a-z0-9_]{1,32}", n)]))
        check("no command is listed twice", len(names) == len(set(names)))
        check("every description fits",
              all(0 < len(d) <= 256 for _, d in COMMAND_MENU))

        app = build_app()
        registered = set()
        for group in app.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    registered |= set(handler.commands)
                for entry in getattr(handler, "entry_points", []) or []:
                    if isinstance(entry, CommandHandler):
                        registered |= set(entry.commands)
                for state in (getattr(handler, "states", {}) or {}).values():
                    for step in state:
                        if isinstance(step, CommandHandler):
                            registered |= set(step.commands)

        missing = sorted(set(names) - registered)
        check("every menu entry has a handler", not missing, str(missing))

        # Deliberately absent: aliases and the steps inside a conversation,
        # which would only pad the menu.
        hidden = {"board", "cancel", "skip", "start"}
        unlisted = sorted(registered - set(names) - hidden)
        check("no command is left out of the menu by accident",
              not unlisted, str(unlisted))
    finally:
        object.__setattr__(CFG, "telegram_token", original)


# --------------------------------------------------------- the untrack picker
async def test_wallet_summary_and_idle() -> None:
    """The picker lists wallets, not wallet-chain pairs, and knows which ones
    have gone quiet everywhere."""
    from memehunter.config import CFG

    original = CFG.db_path
    object.__setattr__(CFG, "db_path",
                       os.path.join(tempfile.mkdtemp(), "picker.db"))
    try:
        conn = await db.connect()
        LIVE = "0x" + "a" * 40
        QUIET = "0x" + "b" * 40
        OTHER = "0x" + "c" * 40
        for chain in ("base", "ethereum", "bsc"):
            await db.add_wallet(conn, 1, chain, LIVE, "winner", "TOK",
                                auto=chain != "base")
            await db.add_wallet(conn, 1, chain, QUIET, None, "TOK",
                                auto=chain != "base")
        await db.add_wallet(conn, 2, "base", OTHER, None, None)

        # QUIET goes dormant everywhere; LIVE keeps one live chain.
        for chain in ("base", "ethereum", "bsc"):
            for _ in range(5):
                await db.record_probe(conn, 1, chain, QUIET, False, 3)
        for _ in range(5):
            await db.record_probe(conn, 1, "ethereum", LIVE, False, 3)

        rows = await db.wallet_summary(conn, 1)
        check("one row per wallet, not per chain", len(rows) == 2, str(len(rows)))
        by = {r["address"]: r for r in rows}
        check("a label survives the grouping", by[LIVE]["label"] == "winner")
        check("every chain is counted", by[LIVE]["chains"] == 3,
              str(by[LIVE]["chains"]))
        check("a wallet with one quiet chain is still live",
              by[LIVE]["live_chains"] == 2, str(by[LIVE]["live_chains"]))
        check("a wallet quiet everywhere has no live chain",
              by[QUIET]["live_chains"] == 0, str(by[QUIET]["live_chains"]))
        check("another chat's wallet is not listed", OTHER not in by)

        idle = await db.idle_wallets(conn, 1)
        check("only the wallet quiet everywhere is idle", idle == [QUIET], str(idle))
        check("another chat's idle list is its own",
              await db.idle_wallets(conn, 2) == [])

        await db.remove_wallet(conn, 1, QUIET)
        check("untracking it empties the idle list",
              await db.idle_wallets(conn, 1) == [])
        await conn.close()
    finally:
        object.__setattr__(CFG, "db_path", original or "memehunter.db")


def test_untrack_keyboard() -> None:
    """Every button has to carry an address and still fit Telegram's 64-byte
    callback limit, which a Solana address nearly fills on its own."""
    from memehunter.config import CFG

    original = CFG.telegram_token
    object.__setattr__(CFG, "telegram_token", "123456:AAdummy-token-for-tests")
    try:
        from memehunter.bot import (UNTRACK_PAGE, untrack_keyboard,
                                    wallet_untrack_keyboard)

        SOLANA = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
        rows = [{"address": SOLANA, "label": "BONK early #1", "live_chains": 1,
                 "chains": 1},
                {"address": "0x" + "a" * 40, "label": None, "live_chains": 0,
                 "chains": 8}]

        markup = untrack_keyboard(rows, page=1, pages=3, idle=7)
        flat = [b for row in markup.inline_keyboard for b in row]
        data = [b.callback_data for b in flat]

        check("one button per wallet, plus paging and the sweep",
              len(flat) == 2 + 2 + 1, str(len(flat)))
        check("every callback fits in 64 bytes",
              all(len(d.encode()) <= 64 for d in data),
              str(max(len(d.encode()) for d in data)))
        check("the address rides in the callback",
              any(d.endswith(SOLANA) for d in data), str(data))
        check("the page rides along too so the list can be redrawn",
              any(d.startswith("unt|1|") for d in data), str(data))
        check("an idle wallet is marked as such",
              any("(idle)" in b.text for b in flat), str([b.text for b in flat]))
        check("there is a way back and a way on",
              "untpg|0" in data and "untpg|2" in data, str(data))
        check("the idle sweep names the count",
              any(b.text == "Untrack all 7 idle wallets" for b in flat))

        first = untrack_keyboard(rows, page=0, pages=1, idle=0)
        data = [b.callback_data for row in first.inline_keyboard for b in row]
        check("a single page offers no paging",
              not any(d.startswith("untpg") for d in data), str(data))
        check("no idle wallets means no sweep button",
              not any(d == "untidle" for d in data), str(data))

        class Card:
            def __init__(self, wallet, label=None):
                self.wallet, self.label = wallet, label

        board = wallet_untrack_keyboard(
            [Card(SOLANA, "BONK early #1"), Card(SOLANA, "BONK early #1"),
             Card("0x" + "d" * 40)])
        data = [b.callback_data for row in board.inline_keyboard for b in row]
        check("the leaderboard offers one button per wallet",
              len(data) == 2, str(data))
        check("a wallet listed twice is only offered once",
              data.count(f"untl|{SOLANA}") == 1, str(data))
        check("leaderboard callbacks fit as well",
              all(len(d.encode()) <= 64 for d in data))
        check("an empty board has no keyboard",
              wallet_untrack_keyboard([]) is None)
        check("a page holds a sensible number of wallets",
              4 <= UNTRACK_PAGE <= 12, str(UNTRACK_PAGE))
    finally:
        object.__setattr__(CFG, "telegram_token", original)


async def test_leaderboard_drops_untracked_wallets() -> None:
    """An untracked wallet must leave the board, while its record survives.

    The board is read to decide what to keep. A wallet already dropped cannot
    be kept or dropped again, so ranking it is noise. Deleting its history
    instead would mean re-tracking it started the grade from nothing.
    """
    from memehunter.config import CFG

    original = CFG.db_path
    object.__setattr__(CFG, "db_path", os.path.join(tempfile.mkdtemp(), "board.db"))
    try:
        conn = await db.connect()
        KEEP, DROP = "0x" + "a" * 40, "0x" + "b" * 40
        for wallet in (KEEP, DROP):
            await db.add_wallet(conn, 1, "base", wallet, None, "TOK")
            await db.record_outcome(conn, 1, "base", wallet, "0xcoin", "COIN",
                                    "BUY", 8.0, 1.0)

        both = await db.outcomes_for_chat(conn, 1, tracked_only=True)
        check("both tracked wallets are on the board", len(both) == 2, str(len(both)))

        await db.remove_wallet(conn, 1, DROP)
        shown = await db.outcomes_for_chat(conn, 1, tracked_only=True)
        check("the untracked wallet leaves the board", len(shown) == 1, str(len(shown)))
        check("the one still tracked stays", shown[0]["wallet"] == KEEP)

        kept = await db.outcomes_for_chat(conn, 1)
        check("its record is not destroyed", len(kept) == 2, str(len(kept)))

        # Re-tracking restores the grade rather than starting it over.
        await db.add_wallet(conn, 1, "base", DROP.upper(), None, "TOK")
        back = await db.outcomes_for_chat(conn, 1, tracked_only=True)
        check("re-tracking brings its history back", len(back) == 2, str(len(back)))
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

    print("\ncommand menu")
    test_command_menu()

    print("\nlong messages")
    test_long_messages_stay_valid_html()

    print("\nprogress safety")
    await test_progress_never_aborts_the_job()

    print("\nname resolution")
    test_dns_override()
    test_dns_parses_a_doh_answer()

    print("\nprovider error paths")
    await test_etherscan_errors_are_not_empty_results()
    test_diagnostics_verdict()

    print("\nbackups")
    await test_backup_survives_a_wipe()

    print("\nmulti-chain tracking")
    await test_empty_wallet_first_buy_alerts()
    await test_dormancy_cycle()
    await test_leaderboard_pools_across_chains()

    print("\ntop traders")
    test_trader_arithmetic()
    test_trader_ranking()
    test_solana_trade_reading()
    await test_solana_trader_scan()

    print("\nuntracking")
    await test_wallet_summary_and_idle()
    test_untrack_keyboard()
    await test_leaderboard_drops_untracked_wallets()
    await test_untrack_forgets_the_wallet()
    test_untrack_accepts_a_pasted_address()
    test_one_instance_per_machine()

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
