"""Shared types, chain detection and a rate-limited HTTP client."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

SOL_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDR = re.compile(r"^0x[a-fA-F0-9]{40}$")

# Etherscan V2: one key, chain selected by chainid.
# Every chain reachable through one Etherscan V2 key.
#
# `dex_ids` are the strings DexScreener may use for the chain in its API. We match
# on a set rather than the key alone because DexScreener names newer chains before
# there is any documented list, and guessing a single spelling wrong would silently
# resolve that chain's tokens to Ethereum and query the wrong contract entirely.
# Add a spelling here if a token on one of these chains resolves oddly.
EVM_CHAINS: dict[str, dict[str, Any]] = {
    "ethereum": {"chainid": 1, "explorer": "https://etherscan.io", "native": "ETH",
                 "decimals": 18, "dex_ids": {"ethereum", "eth", "mainnet"}},
    "base": {"chainid": 8453, "explorer": "https://basescan.org", "native": "ETH",
             "decimals": 18, "dex_ids": {"base"}},
    "bsc": {"chainid": 56, "explorer": "https://bscscan.com", "native": "BNB",
            "decimals": 18, "dex_ids": {"bsc", "binance", "bnb", "binance-smart-chain"}},
    "arbitrum": {"chainid": 42161, "explorer": "https://arbiscan.io", "native": "ETH",
                 "decimals": 18, "dex_ids": {"arbitrum", "arbitrumone", "arb"}},
    "polygon": {"chainid": 137, "explorer": "https://polygonscan.com", "native": "MATIC",
                "decimals": 18, "dex_ids": {"polygon", "matic"}},
    "avalanche": {"chainid": 43114, "explorer": "https://snowscan.xyz", "native": "AVAX",
                  "decimals": 18, "dex_ids": {"avalanche", "avax"}},
    # Arbitrum Orbit L2, launched July 2026. Gas is ETH. Most of its DEX volume
    # runs through memecoins and launchpads rather than the tokenised equities.
    "robinhood": {"chainid": 4663, "explorer": "https://robinhoodchain.blockscout.com",
                  "native": "ETH", "decimals": 18,
                  "dex_ids": {"robinhood", "robinhoodchain", "robinhood-chain",
                              "hood", "rhc"}},
    # Circle's L1, mainnet September 2026. Gas is paid in USDC, not an 18-decimal
    # coin, which is why `decimals` exists on these entries at all.
    "arc": {"chainid": 5042, "explorer": "https://arc-scan.org", "native": "USDC",
            "decimals": 6, "dex_ids": {"arc", "arcmainnet", "arc-mainnet", "circlearc"}},
}

# Reverse lookup: whatever DexScreener called the chain -> our key.
DEX_ID_TO_CHAIN: dict[str, str] = {
    dex_id: chain
    for chain, meta in EVM_CHAINS.items()
    for dex_id in meta["dex_ids"]
}


def chain_from_dex_id(dex_id: str) -> str | None:
    """Map a DexScreener chain string onto a chain we can actually query.

    Returns None when the token lives somewhere we have no provider for. The
    caller must treat that as "unsupported", never as a reason to fall back to
    Ethereum -- querying the wrong chain returns plausible, wrong wallets.
    """
    if not dex_id:
        return None
    key = dex_id.strip().lower().replace("_", "-")
    return DEX_ID_TO_CHAIN.get(key) or DEX_ID_TO_CHAIN.get(key.replace("-", ""))

# Addresses that are never a "buyer": routers, burn holes, common quote tokens.
IGNORE_ADDRESSES = {
    "0x0000000000000000000000000000000000000000",
    "0x000000000000000000000000000000000000dead",
    "0x7a250d5630b4cf539739df2c5dacb4c659f2488d",  # UniV2 router
    "0x68b3465833fb72a70ecdf485e0e4c7bd8665fc45",  # UniV3 router2
    "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad",  # Universal router
    "0x1111111254eeb25477b68fb85ed929f73a960582",  # 1inch v5
    "0x10ed43c718714eb63d5aa57b78b54704e256024e",  # Pancake v2
    "11111111111111111111111111111111",
    "So11111111111111111111111111111111111111112",  # wSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",  # USDT
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "ComputeBudget111111111111111111111111111111",
}
IGNORE_LOWER = {a.lower() for a in IGNORE_ADDRESSES}

# Wrapped native token per chain, used to price the wallet's native balance.
#
# Robinhood Chain and Arc are deliberately absent. Their wrapped-native contract
# addresses are not confirmed here, and a wrong address would price someone's gas
# balance against an unrelated token -- a silently wrong portfolio total is worse
# than an admittedly incomplete one. A chain missing from this map simply has its
# native balance left out of the total; token positions still price normally.
# Add the address here once verified and native pricing turns on with no other change.
WRAPPED_NATIVE = {
    "solana": "So11111111111111111111111111111111111111112",
    "ethereum": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
    "base": "0x4200000000000000000000000000000000000006",
    "bsc": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c",
    "arbitrum": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    "polygon": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270",
    "avalanche": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7",
}

SOL_QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",
    "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
}


def detect_chain(address: str) -> str | None:
    """'solana' for a base58 mint, 'evm' for a 0x address, None if unrecognised."""
    address = address.strip()
    if EVM_ADDR.match(address):
        return "evm"
    if SOL_ADDR.match(address):
        return "solana"
    return None


def short(addr: str, head: int = 4, tail: int = 4) -> str:
    return addr if len(addr) <= head + tail + 1 else f"{addr[:head]}..{addr[-tail:]}"


def extract_addresses(text: str) -> list[str]:
    """Every wallet-shaped string in a pasted blob, in order, without duplicates.

    Lets someone dump a column of addresses from a spreadsheet or an explorer and
    have all of them picked up, rather than typing one command per wallet.
    """
    found: list[str] = []
    for word in re.split(r"[\s,;|]+", text or ""):
        word = word.strip().strip(".,:()[]<>\"'")
        # A trailing path segment from a pasted explorer URL is still an address.
        if "/" in word:
            word = word.rstrip("/").rsplit("/", 1)[-1]
        if (EVM_ADDR.match(word) or SOL_ADDR.match(word)) and word not in found:
            found.append(word)
    return found


@dataclass
class EarlyBuyer:
    address: str
    rank: int
    first_ts: int
    chain: str
    tx_count_at_entry: int = 0


@dataclass
class WalletProfile:
    address: str
    chain: str
    last_active_ts: int = 0
    tx_count_30d: int = 0
    median_gap_sec: float = 0.0
    min_gap_sec: float = 0.0
    is_bot: bool = False
    bot_reason: str = ""
    dead: bool = False

    @property
    def days_since_active(self) -> float:
        if not self.last_active_ts:
            return 9e9
        return (time.time() - self.last_active_ts) / 86400


@dataclass
class Holding:
    token: str
    symbol: str = ""
    amount: float = 0.0
    first_seen_ts: int = 0


@dataclass
class TokenMarket:
    """Normalised DexScreener view of a token's best pair."""
    address: str
    chain: str
    symbol: str = ""
    name: str = ""
    price_usd: float = 0.0
    liquidity_usd: float = 0.0
    fdv: float = 0.0
    volume_24h: float = 0.0
    price_change_24h: float = 0.0
    price_change_6h: float = 0.0
    buys_24h: int = 0
    sells_24h: int = 0
    pair_created_ms: int = 0
    pair_url: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def age_days(self) -> float:
        if not self.pair_created_ms:
            return 9e9
        return (time.time() - self.pair_created_ms / 1000) / 86400


class RateLimiter:
    """Simple async token bucket: at most `rate` calls per `per` seconds."""

    def __init__(self, rate: int, per: float = 1.0):
        self.rate, self.per = rate, per
        self._allowance = float(rate)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._allowance += (now - self._last) * (self.rate / self.per)
                self._last = now
                if self._allowance > self.rate:
                    self._allowance = float(self.rate)
                if self._allowance >= 1:
                    self._allowance -= 1
                    return
                await asyncio.sleep((1 - self._allowance) * (self.per / self.rate))


class HttpClient:
    """httpx wrapper with a rate limiter and retry/backoff on 429 and 5xx."""

    def __init__(self, rate: int = 5, per: float = 1.0, timeout: float = 30.0):
        self._limiter = RateLimiter(rate, per)
        self._client = httpx.AsyncClient(
            timeout=timeout, headers={"User-Agent": "memehunter-bot/1.0"}
        )

    async def request(self, method: str, url: str, *, retries: int = 3, **kw) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries):
            await self._limiter.acquire()
            try:
                resp = await self._client.request(method, url, **kw)
                if resp.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(1.5 * (2 ** attempt))
                    last_exc = httpx.HTTPStatusError(
                        f"{resp.status_code} from {url}", request=resp.request, response=resp
                    )
                    continue
                resp.raise_for_status()
                return resp.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_exc = exc
                await asyncio.sleep(1.0 * (2 ** attempt))
        raise last_exc or RuntimeError(f"request failed: {url}")

    async def get(self, url: str, **kw) -> Any:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Any:
        return await self.request("POST", url, **kw)

    async def aclose(self) -> None:
        await self._client.aclose()
