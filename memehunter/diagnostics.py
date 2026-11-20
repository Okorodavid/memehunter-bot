"""One command that tests every data source and says what is actually broken.

Every provider in this bot degrades quietly: Etherscan answers a rejected key
with the same shape it uses for "no results", Solscan answers one with a bare
401, and the Solana RPC just returns nothing. Each failure then surfaces as
"no early buyers found", which reads like a fact about the coin rather than a
broken credential.

This asks each API a question it must be able to answer, and reports what came
back. A key that works says so; one that does not says why, in the API's own
words.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import httpx

from . import dns
from .config import CFG
from .providers.base import EVM_CHAINS
from .providers.evm import EvmProvider
from .providers.prices import DexScreener
from .providers.solana import SolanaProvider

# Things every chain definitely has, so a failure is never about the subject.
WSOL = "So11111111111111111111111111111111111111112"
PROBE_TOKEN = {
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",   # WETH
    "base": "0x4200000000000000000000000000000000000006",        # WETH on Base
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",         # WBNB
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "polygon": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "avalanche": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    skipped: bool = False

    @property
    def mark(self) -> str:
        if self.skipped:
            return "--"
        return "ok" if self.ok else "!!"


async def _telegram() -> Check:
    """Telegram itself, which nothing else in the bot can work without.

    This is here because a poisoned `api.telegram.org` killed the bot at
    startup while every data source was healthy. The failure looked like the
    bot being offline, and nothing in /diag contradicted that.
    """
    name = "Telegram"
    if not CFG.telegram_token:
        return Check(name, False, "no TELEGRAM_BOT_TOKEN set", skipped=True)
    ok, detail = dns.check("api.telegram.org")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                f"https://api.telegram.org/bot{CFG.telegram_token}/getMe")
        body = resp.json()
    except Exception as exc:
        return Check(name, False, f"unreachable ({exc}). DNS: {detail}")
    if body.get("ok"):
        return Check(name, True, f"@{(body.get('result') or {}).get('username', '?')}")
    return Check(name, False, f"{body.get('description') or resp.status_code}")


async def _dexscreener(dex: DexScreener) -> Check:
    try:
        market = await dex.market(WSOL)
    except Exception as exc:
        return Check("DexScreener", False, f"unreachable ({exc})")
    if market and market.price_usd > 0:
        return Check("DexScreener", True, f"SOL at ${market.price_usd:,.2f}")
    return Check("DexScreener", False, "no price came back for wrapped SOL")


async def _solana_rpc(sol: SolanaProvider) -> Check:
    label = "Helius RPC" if CFG.helius_key else "public Solana RPC"
    try:
        slot = await sol.rpc("getSlot", [])
    except Exception as exc:
        return Check(label, False, f"{exc}")
    if isinstance(slot, int) and slot > 0:
        return Check(label, True, f"at slot {slot:,}")
    return Check(label, False, "no slot returned")


async def _solscan(sol: SolanaProvider) -> Check:
    if not CFG.solscan_key:
        return Check("Solscan", False, "no key set - optional, speeds up big coins",
                     skipped=True)
    try:
        data = await sol._solscan_get("/token/meta", {"address": WSOL})
    except Exception as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (401, 403):
            return Check("Solscan", False,
                         f"key rejected ({code}) - check solscan.io/user/profile"
                         f"#api_management")
        return Check("Solscan", False, f"{exc}")
    if isinstance(data, dict) and (data.get("data") or data.get("success")):
        header = next(iter(sol._solscan_headers or {"?": ""}))
        return Check("Solscan", True, f"key accepted via the '{header}' header")
    return Check("Solscan", False, "responded, but with no data")


async def _etherscan(chain: str) -> Check:
    name = f"Etherscan / {chain}"
    prov = EvmProvider(chain)
    if not prov.enabled:
        await prov.aclose()
        return Check(name, False, "no ETHERSCAN_API_KEY set", skipped=True)
    token = PROBE_TOKEN.get(chain)
    try:
        if token:
            # The wrapped-native contract's own balance: a cheap question every
            # plan and every explorer answers, and one that never comes back
            # empty. (A transfer listing would spend Blockscout's scarce
            # keyless quota on a test.)
            rows = await prov._call("account", "balance", address=token, tag="latest")
        else:
            # No known probe token on a new chain; ask for the block number,
            # which every chain answers and which needs no subject at all.
            rows = await prov._call("proxy", "eth_blockNumber")
    finally:
        await prov.aclose()

    # Decided after the call: Etherscan may have handed the chain over mid-way.
    name = f"{prov.source} / {chain}"
    if prov.last_error:
        return Check(name, False, prov.last_error)
    if rows and str(rows) not in ("0", "[]"):
        return Check(name, True, f"chainid {prov.chainid}, answered")
    return Check(name, False, "reachable but returned nothing for a known contract")


async def run(analyzer, chains: list[str] | None = None) -> tuple[list[Check], float]:
    """Test everything. Returns the checks and how long they took."""
    started = time.monotonic()
    chains = chains or ["ethereum", "base", "bsc"]

    checks = [
        await _telegram(),
        await _dexscreener(analyzer.dex),
        await _solana_rpc(analyzer.sol),
        await _solscan(analyzer.sol),
    ]
    checks.extend(await asyncio.gather(*(_etherscan(c) for c in chains)))
    return checks, time.monotonic() - started


def verdict(checks: list[Check]) -> str:
    """What the results mean for whether the bot can do its job."""
    broken = [c for c in checks if not c.ok and not c.skipped]
    if not broken:
        return "Everything the bot needs is working."

    names = ", ".join(c.name for c in broken)
    if any(c.name == "Telegram" for c in broken):
        return (f"<b>{names}</b> failed. Telegram is how this bot exists at all - "
                f"if that line is down nothing else matters, and the bot cannot "
                f"start at all once it drops.")
    core = [c for c in broken
            if c.name.startswith("DexScreener") or "RPC" in c.name]
    if core:
        return (f"<b>{names}</b> failed. DexScreener and the Solana RPC are not "
                f"optional - without them nothing works, not even scoring.")
    return (f"<b>{names}</b> failed. Anything on those chains will come back "
            f"empty, and that emptiness is this failure, not a verdict on a coin.")
