"""Offline checks for the pipeline logic. No network, no API keys: python selftest.py"""
from __future__ import annotations

import asyncio
import os
import tempfile
import time

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "selftest.db"))

from memehunter import db, render                                     # noqa: E402
from memehunter.pipeline.analyzer import Analyzer                     # noqa: E402
from memehunter.pipeline.scoring import score_token                   # noqa: E402
from memehunter.providers.base import (Holding, TokenMarket,          # noqa: E402
                                       WalletProfile, detect_chain,
                                       extract_addresses)
from memehunter.providers.solana import SolanaProvider, classify_bot  # noqa: E402
from memehunter.bot import harvest_labels                             # noqa: E402
import tests_exits                                                    # noqa: E402

PASSED = FAILED = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name} {detail}")


def market(**kw) -> TokenMarket:
    base = dict(
        address="TOK", chain="solana", symbol="TEST", price_usd=0.000123,
        liquidity_usd=80_000, fdv=900_000, volume_24h=200_000,
        price_change_24h=40, price_change_6h=12, buys_24h=800, sells_24h=400,
        pair_created_ms=int((time.time() - 3 * 86400) * 1000),
    )
    base.update(kw)
    return TokenMarket(**base)


SAFE = {"mint_authority": None, "freeze_authority": None, "top10_share_ex_largest": 0.15}


def test_chain_detection() -> None:
    print("chain detection")
    check("solana mint", detect_chain("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263") == "solana")
    check("evm address", detect_chain("0x" + "a" * 40) == "evm")
    check("garbage rejected", detect_chain("not-an-address") is None)
    check("evm case insensitive", detect_chain("0x" + "A" * 40) == "evm")


def test_address_extraction() -> None:
    print("address extraction (manual add)")
    sol = "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"
    evm = "0x" + "a" * 40
    check("single address", extract_addresses(sol) == [sol])
    check("newline separated", extract_addresses(f"{sol}\n{evm}") == [sol, evm])
    check("comma separated", extract_addresses(f"{sol}, {evm}") == [sol, evm])
    check("explorer url unwrapped",
          extract_addresses(f"https://solscan.io/account/{sol}") == [sol])
    check("duplicates collapsed", extract_addresses(f"{sol} {sol}") == [sol])
    check("surrounding prose ignored",
          extract_addresses(f"track this one ({sol}) please") == [sol])
    check("no false positives", extract_addresses("hello there, no addresses") == [])
    check("empty input safe", extract_addresses("") == [])
    check("order preserved", extract_addresses(f"{evm}\n{sol}") == [evm, sol])


def test_harvest_labels() -> None:
    print("harvest labelling")
    labels = harvest_labels("BONK", ["w1", "w2", "w3"])
    check("one label per wallet", len(labels) == 3)
    check("entry order preserved", labels["w1"].endswith("#1")
          and labels["w3"].endswith("#3"))
    check("named after the source coin", labels["w1"].startswith("BONK"))
    check("missing symbol degrades safely", harvest_labels("", ["w"])["w"] == "? early #1")
    check("absurd symbol truncated",
          len(harvest_labels("X" * 80, ["w"])["w"]) < 30)
    check("empty cohort is empty", harvest_labels("BONK", []) == {})


def test_scoring() -> None:
    print("scoring")
    ideal = score_token(market(), overlap_wallets=5, safety=SAFE, chain="solana")
    check("ideal setup is buy-grade", ideal.total >= 9.0, f"got {ideal.total}")
    check("ideal has no flags", not ideal.flags, str(ideal.flags))
    check("components sum to total",
          abs(sum(c.points for c in ideal.components) - ideal.total) < 0.01)
    check("never exceeds 10", ideal.total <= 10.0)

    no_overlap = score_token(market(), 0, SAFE, "solana")
    check("no overlap costs 3 points", abs(ideal.total - no_overlap.total - 3.0) < 0.01,
          f"{ideal.total} vs {no_overlap.total}")

    rugpull = score_token(
        market(), 4,
        {"mint_authority": "SomeAuthority", "freeze_authority": "Auth",
         "top10_share_ex_largest": 0.72},
        "solana")
    check("live mint authority flags", any("mint" in f for f in rugpull.flags))
    check("live freeze authority flags", any("freeze" in f for f in rugpull.flags))
    check("concentration flags", any("top 10" in f for f in rugpull.flags))
    check("flags force AVOID", rugpull.verdict == "AVOID", rugpull.verdict)

    dust = score_token(market(liquidity_usd=1_200), 3, SAFE, "solana")
    check("untradeable liquidity flags", any("untradeable" in f for f in dust.flags))

    dumping = score_token(market(buys_24h=100, sells_24h=900), 3, SAFE, "solana")
    check("net selling flags", any("selling" in f for f in dumping.flags))

    missing = score_token(None, 3, SAFE, "solana")
    check("no market scores zero", missing.total == 0 and missing.verdict == "AVOID")

    evm = score_token(market(chain="base"), 3,
                      {"verified": False, "proxy": True}, "base")
    check("unverified evm contract flags", any("verified" in f for f in evm.flags))
    check("proxy flags", any("proxy" in f for f in evm.flags))

    old = score_token(market(pair_created_ms=int((time.time() - 900 * 86400) * 1000)),
                      4, SAFE, "solana")
    check("old coin scores below fresh one", old.total < ideal.total)


def test_bot_filter() -> None:
    print("bot filter (step 4)")
    human = WalletProfile(address="w", chain="solana", tx_count_30d=40,
                          median_gap_sec=3600, min_gap_sec=120)
    classify_bot(human)
    check("human not flagged", not human.is_bot, human.bot_reason)

    sniper = WalletProfile(address="w", chain="solana", tx_count_30d=500,
                           median_gap_sec=0.4, min_gap_sec=0.1)
    classify_bot(sniper)
    check("fast trader flagged", sniper.is_bot)
    check("bot reason recorded", "gap" in sniper.bot_reason, sniper.bot_reason)

    grinder = WalletProfile(address="w", chain="solana", tx_count_30d=20_000,
                            median_gap_sec=60, min_gap_sec=30)
    classify_bot(grinder)
    check("high-volume wallet flagged", grinder.is_bot, grinder.bot_reason)


def test_balance_parsing() -> None:
    print("transaction parsing")
    tx = {"meta": {
        "preTokenBalances": [
            {"owner": "alice", "mint": "TOK", "uiTokenAmount": {"uiAmount": 0}},
        ],
        "postTokenBalances": [
            {"owner": "alice", "mint": "TOK", "uiTokenAmount": {"uiAmount": 500}},
            {"owner": "bob", "mint": "TOK", "uiTokenAmount": {"uiAmount": 0}},
        ],
    }}
    gained = SolanaProvider._owners_gaining(tx, "TOK")
    check("buyer detected", gained == ["alice"], str(gained))
    check("zero-balance account ignored", "bob" not in gained)
    check("null tx handled", SolanaProvider._owners_gaining(None, "TOK") == [])

    deltas = dict(SolanaProvider._mint_deltas_for_owner(tx, "alice"))
    check("delta computed", deltas.get("TOK") == 500, str(deltas))
    check("seller produces no delta",
          SolanaProvider._mint_deltas_for_owner(
              {"meta": {
                  "preTokenBalances": [
                      {"owner": "alice", "mint": "TOK",
                       "uiTokenAmount": {"uiAmount": 500}}],
                  "postTokenBalances": [
                      {"owner": "alice", "mint": "TOK",
                       "uiTokenAmount": {"uiAmount": 0}}],
              }}, "alice") == [])


async def test_holdings_parsing() -> None:
    """The watcher lives or dies on this parse: a wrong balance is a false alert."""
    print("holdings parsing")
    sol = SolanaProvider()

    def account(mint: str, amount: float):
        return {"account": {"data": {"parsed": {"info": {
            "mint": mint, "tokenAmount": {"uiAmount": amount}}}}}}

    async def fake_batch(calls):
        return [
            {"value": [
                account("TOK1", 100.0),
                account("TOK2", 0.0),                       # emptied account
                account("TOK1", 50.0),                      # second account, same mint
                account("So11111111111111111111111111111111111111112", 5.0),
            ]},
            {"value": [account("TOK22", 7.0)]},             # Token-2022 program
        ]

    sol.rpc_batch = fake_batch
    holdings = {h.token: h.amount for h in await sol.wallet_holdings("W")}
    check("balances across accounts of one mint are summed",
          holdings.get("TOK1") == 150.0, str(holdings))
    check("zero-balance account excluded", "TOK2" not in holdings)
    check("wrapped SOL excluded from token holdings",
          "So11111111111111111111111111111111111111112" not in holdings)
    check("token-2022 balances included", holdings.get("TOK22") == 7.0)
    await sol.aclose()


async def test_overlap() -> None:
    print("overlap (step 6)")
    az = Analyzer()
    markets = {t: market(address=t, symbol=t) for t in ("A", "B", "C")}

    async def fake_markets(addresses):
        return {a: markets[a] for a in addresses if a in markets}

    async def fake_safety(_token):
        return SAFE

    az.dex.markets = fake_markets
    az.sol.token_safety = fake_safety

    buys = {
        "w1": [Holding(token="A"), Holding(token="B"), Holding(token="SEED")],
        "w2": [Holding(token="A"), Holding(token="B")],
        "w3": [Holding(token="A"), Holding(token="C")],
    }
    cands = await az.overlap_and_score("solana", buys, exclude={"SEED"})
    by_token = {c.token: c for c in cands}
    check("3-wallet overlap surfaces", "A" in by_token)
    check("A credited to all three wallets", len(by_token["A"].wallets) == 3)
    check("2-wallet token below threshold excluded from top",
          by_token["A"].score.total > by_token.get("B", cands[-1]).score.total
          if "B" in by_token else True)
    check("excluded seed token dropped", "SEED" not in by_token)
    check("results sorted by score",
          all(cands[i].score.total >= cands[i + 1].score.total
              for i in range(len(cands) - 1)))

    lonely = await az.overlap_and_score(
        "solana", {"w1": [Holding(token="A")]}, exclude=set())
    check("single-wallet token yields nothing", lonely == [], str(lonely))
    await az.aclose()


async def test_db() -> None:
    print("persistence")
    conn = await db.connect()
    added = await db.add_wallet(conn, 1, "solana", "WALLET1", "whale", "TOK")
    again = await db.add_wallet(conn, 1, "solana", "WALLET1", "whale", "TOK")
    check("wallet added", added)
    check("duplicate rejected", not again)

    rows = await db.list_wallets(conn, 1)
    check("wallet listed", len(rows) == 1 and rows[0]["label"] == "whale")
    check("other chat sees nothing", len(await db.list_wallets(conn, 2)) == 0)

    check("no snapshot before first save",
          not await db.has_snapshot(conn, "solana", "WALLET1"))
    await db.save_snapshot(conn, "solana", "WALLET1", {"A": 100.0, "B": 50.0})
    check("snapshot recorded", await db.has_snapshot(conn, "solana", "WALLET1"))
    snap = await db.position_snapshot(conn, "solana", "WALLET1")
    check("amounts stored", snap["A"]["amount"] == 100.0, str(snap))

    await db.save_snapshot(conn, "solana", "WALLET1", {"A": 250.0})
    snap = await db.position_snapshot(conn, "solana", "WALLET1")
    check("balance updated", snap["A"]["amount"] == 250.0)
    check("peak tracks the high-water mark", snap["A"]["peak"] == 250.0)
    check("dropped token zeroed, not deleted", snap["B"]["amount"] == 0.0)
    check("peak of a closed position is remembered", snap["B"]["peak"] == 50.0)
    check("known_positions excludes closed ones",
          await db.known_positions(conn, "solana", "WALLET1") == {"A"})

    await db.save_snapshot(conn, "solana", "WALLET1", {"A": 100.0})
    snap = await db.position_snapshot(conn, "solana", "WALLET1")
    check("peak survives a drawdown", snap["A"]["peak"] == 250.0)

    await db.save_snapshot(conn, "solana", "WALLET1", {})
    check("empty snapshot closes everything",
          await db.known_positions(conn, "solana", "WALLET1") == set())

    check("first buy alert allowed",
          await db.should_alert(conn, 1, "solana", "WALLET1", "A", "BUY"))
    check("repeat buy alert suppressed",
          not await db.should_alert(conn, 1, "solana", "WALLET1", "A", "BUY"))
    check("an exit on the same position still alerts",
          await db.should_alert(conn, 1, "solana", "WALLET1", "A", "EXIT"))
    check("buying back in alerts again",
          await db.should_alert(conn, 1, "solana", "WALLET1", "A", "BUY"))

    await db.set_watch(conn, 1, True)
    check("watch list picks up chat", 1 in await db.all_watching_chats(conn))
    await db.set_min_score(conn, 1, 8.5)
    settings = await db.get_settings(conn, 1)
    check("min score stored", settings["min_score"] == 8.5)
    check("set_min_score preserves watch flag", settings["watch"] == 1)

    await db.cache_set(conn, "k", {"v": 1}, ttl=60)
    check("cache round trip", (await db.cache_get(conn, "k")) == {"v": 1})
    await db.cache_set(conn, "expired", 1, ttl=-10)
    check("expired cache entry ignored", await db.cache_get(conn, "expired") is None)

    # Harvesting a second coin must merge into the existing list, never duplicate
    # or overwrite a wallet that two cohorts share.
    check("wallet harvested from a second coin is not re-added",
          not await db.add_wallet(conn, 1, "solana", "WALLET1", "OTHER early #2", "TOK2"))
    check("original label survives the merge",
          (await db.list_wallets(conn, 1))[0]["label"] == "whale")
    check("still exactly one row", len(await db.list_wallets(conn, 1)) == 1)

    check("relabel works", await db.set_label(conn, 1, "wallet1", "renamed") == 1)
    check("label persisted",
          (await db.list_wallets(conn, 1))[0]["label"] == "renamed")
    check("relabel of unknown wallet is a no-op",
          await db.set_label(conn, 1, "NOPE", "x") == 0)

    check("untrack works", await db.remove_wallet(conn, 1, "wallet1") == 1)
    await conn.close()


def test_render() -> None:
    print("rendering")
    check("sub-cent price is legible", render.usd(0.000123) == "$0.000123",
          render.usd(0.000123))
    check("thousands abbreviated", render.usd(219_438) == "$219.4K")
    check("millions abbreviated", render.usd(285_290_000) == "$285.29M")
    check("html escaped", "&lt;script&gt;" in render.esc("<script>"))
    check("long text clipped", len(render.clip("x" * 9000)) < 4100)

    s = score_token(market(), 4, SAFE, "solana")
    body = render.render_score(s)
    check("score renders bar and verdict", "BUY-GRADE" in body and "#" in body)


async def main() -> int:
    test_chain_detection()
    test_address_extraction()
    test_harvest_labels()
    test_scoring()
    test_bot_filter()
    test_balance_parsing()
    await test_holdings_parsing()
    await test_overlap()
    await test_db()
    test_render()
    tests_exits.run(check)
    print(f"\n{PASSED} passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
