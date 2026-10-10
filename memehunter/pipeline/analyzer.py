"""The seven steps, wired end to end.

  1. you pick a coin from a previous cycle        -> caller supplies the address
  2. identify the top N early buyers              -> provider.early_buyers
  3. keep only wallets active in the last 30 days -> provider.wallet_profile
  4. drop the bots                                -> classify_bot heuristics
  5. see what the survivors bought recently       -> provider.wallet_recent_buys
  6. find tokens that repeat across wallets       -> overlap()
  7. score every candidate                        -> scoring.score_token
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import CFG
from ..providers.base import (EVM_CHAINS, EarlyBuyer, Holding, TokenMarket,
                              chain_from_dex_id,
                              WalletProfile, detect_chain)
from ..providers.evm import EvmProvider
from ..providers.prices import DexScreener
from ..providers.solana import HistoryTooDeep, SolanaProvider
from .portfolio import Portfolio, build_portfolio
from .scoring import Score, score_token

Progress = Callable[[str], Awaitable[None]]

WALLET_CONCURRENCY = 4


async def _noop(_: str) -> None:
    return None


@dataclass
class Candidate:
    token: str
    chain: str
    wallets: list[str] = field(default_factory=list)
    market: TokenMarket | None = None
    score: Score | None = None

    @property
    def symbol(self) -> str:
        return (self.market.symbol if self.market else "") or self.token[:6]


@dataclass
class TokenAnalysis:
    token: str
    chain: str
    market: TokenMarket | None = None
    early_buyers: list[EarlyBuyer] = field(default_factory=list)
    profiles: list[WalletProfile] = field(default_factory=list)
    survivors: list[WalletProfile] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    error: str = ""
    # True when no market data existed and the chain is a default, not a fact.
    chain_guessed: bool = False

    @property
    def dead_count(self) -> int:
        return sum(1 for p in self.profiles if p.dead)

    @property
    def bot_count(self) -> int:
        return sum(1 for p in self.profiles if p.is_bot and not p.dead)


class Analyzer:
    def __init__(self) -> None:
        self.sol = SolanaProvider()
        self.dex = DexScreener()
        self._evm: dict[str, EvmProvider] = {}

    def evm(self, chain: str) -> EvmProvider:
        if chain not in self._evm:
            self._evm[chain] = EvmProvider(chain)
        return self._evm[chain]

    def provider(self, chain: str):
        return self.sol if chain == "solana" else self.evm(chain)

    # ------------------------------------------------------------------ helpers
    async def resolve_chain(self, address: str, chain_hint: str | None = None
                            ) -> tuple[str, TokenMarket | None, bool]:
        """Which chain is this address on, what does the market look like, and
        was the chain actually known or merely guessed?

        The third value matters. An EVM address is valid on every EVM chain, so
        a wrong guess queries a real lookup on the wrong network and comes back
        empty -- indistinguishable from "this coin has no early buyers" unless
        the caller can say which it was.
        """
        kind = detect_chain(address)
        if kind is None:
            raise ValueError("That does not look like a Solana mint or an EVM address.")
        market = await self.dex.market(address)
        if kind == "solana":
            return "solana", market, True

        # An explicit chain from the user beats anything inferred.
        if chain_hint and chain_hint in EVM_CHAINS:
            return chain_hint, market, True

        if market and market.chain:
            # An unrecognised chain is returned as-is so the caller can name it.
            return chain_from_dex_id(market.chain) or market.chain, market, True

        # No pair anywhere: too new, delisted, not a token, or simply not
        # indexed. Ethereum is the only sane default, but it is a guess and the
        # caller must say so rather than reporting an empty result as fact.
        return "ethereum", market, False

    def explorer_token(self, chain: str, token: str) -> str:
        if chain == "solana":
            return f"https://solscan.io/token/{token}"
        meta = EVM_CHAINS.get(chain)
        if not meta:
            # An unsupported chain can still reach rendering via an error path.
            # A DexScreener link always resolves, so it beats crashing on a
            # missing explorer.
            return f"https://dexscreener.com/{chain}/{token}"
        return f"{meta['explorer']}/token/{token}"

    def explorer_wallet(self, chain: str, wallet: str) -> str:
        if chain == "solana":
            return f"https://solscan.io/account/{wallet}"
        meta = EVM_CHAINS.get(chain)
        if not meta:
            return f"https://blockscan.com/address/{wallet}"
        return f"{meta['explorer']}/address/{wallet}"

    # ------------------------------------------------------- steps 2 -> 4 only
    async def find_smart_wallets(
        self, token: str, progress: Progress = _noop, limit: int | None = None,
        chain_hint: str | None = None,
    ) -> TokenAnalysis:
        limit = limit or CFG.early_buyer_count
        chain, market, certain = await self.resolve_chain(token, chain_hint)
        res = TokenAnalysis(token=token, chain=chain, market=market)
        res.chain_guessed = not certain

        if chain != "solana" and chain not in EVM_CHAINS:
            res.error = (
                f"That token trades on {chain}, which this bot cannot query. "
                f"Supported: solana, {', '.join(sorted(EVM_CHAINS))}."
            )
            return res
        if chain != "solana" and not self.evm(chain).enabled:
            res.error = (
                f"ETHERSCAN_API_KEY is not set and {chain} has no keyless public "
                f"explorer, so it is unavailable. A key from etherscan.io/apis is "
                f"needed, on a plan that includes {chain}."
            )
            return res

        prov = self.provider(chain)
        await progress(f"Step 2 - pulling the first {limit} buyers on {chain}...")
        try:
            if chain == "solana":
                res.early_buyers = await prov.early_buyers(token, limit)
            else:
                # When the pair appeared tells a keyless explorer where in the
                # chain's history to start reading.
                since = int(market.pair_created_ms / 1000) if market and market.pair_created_ms else 0
                res.early_buyers = await prov.early_buyers(token, limit, since_ts=since)
        except HistoryTooDeep as exc:
            res.error = str(exc)
            return res
        if not res.early_buyers:
            # An API that refused the request is not a coin without buyers.
            api_error = getattr(prov, "last_error", "")
            if api_error:
                res.error = (
                    f"The {chain} lookup failed, so this is not a verdict on the "
                    f"coin.\n\n<b>{api_error}</b>\n\n"
                    f"Run /diag to test every API key at once."
                )
            elif res.chain_guessed:
                res.error = (
                    f"No early buyers found on <b>{chain}</b>.\n\n"
                    f"DexScreener has no pair for this token, so I could not tell "
                    f"which chain it is on and defaulted to {chain}. If it is "
                    f"somewhere else, say so:\n"
                    f"<code>/harvest {token} base</code>\n\n"
                    f"Chains: {', '.join(sorted(EVM_CHAINS))}."
                )
            else:
                res.error = ("No early buyers found on "
                             f"{chain}. The address may be wrong or too new.")
            return res

        await progress(
            f"Step 3/4 - profiling {len(res.early_buyers)} wallets "
            f"(activity + bot check)..."
        )
        sem = asyncio.Semaphore(WALLET_CONCURRENCY)

        async def profile(buyer: EarlyBuyer) -> WalletProfile:
            async with sem:
                try:
                    return await prov.wallet_profile(buyer.address)
                except Exception:
                    p = WalletProfile(address=buyer.address, chain=chain)
                    p.dead = True
                    return p

        res.profiles = list(await asyncio.gather(*(profile(b) for b in res.early_buyers)))
        res.survivors = [p for p in res.profiles if not p.dead and not p.is_bot]
        return res

    # ----------------------------------------------------------- steps 5 -> 7
    async def analyze_token(
        self, token: str, progress: Progress = _noop, limit: int | None = None,
        chain_hint: str | None = None,
    ) -> TokenAnalysis:
        res = await self.find_smart_wallets(token, progress, limit, chain_hint)
        if res.error or not res.survivors:
            return res

        await progress(
            f"Step 5 - checking what {len(res.survivors)} surviving wallets "
            f"bought in the last {CFG.recent_buy_window_days} days..."
        )
        buys = await self.recent_buys(res.chain, [p.address for p in res.survivors])

        await progress("Steps 6/7 - finding repeats and scoring them...")
        res.candidates = await self.overlap_and_score(
            res.chain, buys, exclude={token.lower(), token}
        )
        return res

    async def recent_buys(
        self, chain: str, wallets: list[str]
    ) -> dict[str, list[Holding]]:
        prov = self.provider(chain)
        sem = asyncio.Semaphore(WALLET_CONCURRENCY)

        async def one(w: str) -> tuple[str, list[Holding]]:
            async with sem:
                try:
                    return w, await prov.wallet_recent_buys(w, CFG.recent_buy_window_days)
                except Exception:
                    return w, []

        return dict(await asyncio.gather(*(one(w) for w in wallets)))

    async def holdings(self, chain: str, wallets: list[str]) -> dict[str, list[Holding]]:
        """Current balances for several wallets at once.

        Cheaper than replaying transaction history -- two RPC calls per Solana wallet
        -- which is what lets the watcher poll often enough for exits to be useful.
        """
        prov = self.provider(chain)
        sem = asyncio.Semaphore(WALLET_CONCURRENCY)

        async def one(w: str) -> tuple[str, list[Holding]]:
            async with sem:
                try:
                    return w, await prov.wallet_holdings(w)
                except Exception:
                    return w, []

        return dict(await asyncio.gather(*(one(w) for w in wallets)))

    async def portfolio(self, chain: str, wallet: str,
                        holdings: list[Holding] | None = None) -> Portfolio:
        return await build_portfolio(self, chain, wallet, holdings)

    async def overlap_and_score(
        self,
        chain: str,
        buys: dict[str, list[Holding]],
        exclude: set[str] | None = None,
        min_wallets: int | None = None,
    ) -> list[Candidate]:
        """Step 6 + 7: a token in one wallet is luck, in three it is a signal."""
        exclude = exclude or set()
        min_wallets = min_wallets or CFG.overlap_min_wallets

        holders: dict[str, set[str]] = defaultdict(set)
        for wallet, holdings in buys.items():
            for h in holdings:
                if h.token in exclude or h.token.lower() in exclude:
                    continue
                holders[h.token].add(wallet)

        repeated = {t: w for t, w in holders.items() if len(w) >= min_wallets}
        # If nothing clears the bar, still surface the best near-misses.
        if not repeated:
            repeated = {
                t: w for t, w in sorted(
                    holders.items(), key=lambda kv: len(kv[1]), reverse=True
                )[:5] if len(w) >= 2
            }
        if not repeated:
            return []

        markets = await self.dex.markets(list(repeated))
        prov = self.provider(chain)
        sem = asyncio.Semaphore(WALLET_CONCURRENCY)

        async def build(token: str, wallets: set[str]) -> Candidate:
            market = markets.get(token) or markets.get(token.lower())
            async with sem:
                try:
                    safety = await prov.token_safety(token)
                except Exception:
                    safety = {}
            cand = Candidate(token=token, chain=chain, wallets=sorted(wallets),
                             market=market)
            cand.score = score_token(market, len(wallets), safety, chain)
            return cand

        cands = list(await asyncio.gather(
            *(build(t, w) for t, w in list(repeated.items())[:25])))
        cands.sort(key=lambda c: (c.score.total if c.score else 0), reverse=True)
        return cands

    async def score_one(self, token: str, overlap_wallets: int = 0) -> tuple[Candidate, str]:
        chain, market, _ = await self.resolve_chain(token)
        prov = self.provider(chain)
        try:
            safety = await prov.token_safety(token)
        except Exception:
            safety = {}
        cand = Candidate(token=token, chain=chain, market=market)
        cand.score = score_token(market, overlap_wallets, safety, chain)
        return cand, chain

    async def aclose(self) -> None:
        await asyncio.gather(
            self.sol.aclose(), self.dex.aclose(),
            *(p.aclose() for p in self._evm.values()),
            return_exceptions=True,
        )
