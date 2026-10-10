"""Who actually made money on a coin, and what they risked to make it.

The seven-step method asks who was *early*. This asks a different question:
who turned a small position into a large one. A wallet that put in twenty
dollars and took out two thousand is saying something an entry timestamp
cannot -- it sized small, it sold, and it was right.

Both chains answer the same question from the same shape of data: for each
wallet, how much of the coin went in and out, and how much of a quote asset
went the other way. Only the sourcing differs.

  Solana  the token's own transaction history, read oldest-first and
          newest-first, with each transaction's per-owner balance deltas.
          One pass covers every trader the coin ever had.

  EVM     one call per wallet for that wallet's transfers of the token, then
          the other legs of those transactions to see what was paid. Reading
          the whole token's history is not possible on a keyless node: Base
          caps `eth_getLogs` near 2,000 blocks, and a Uniswap v4 coin has no
          pair contract to filter on. So the field is the early-buyer cohort
          rather than everyone, and the result says so.

Prices are today's, applied to historical trades. For a stablecoin quote that
is exact. For SOL or ETH it is not, and the multiple -- which is computed in
the quote asset, before any conversion -- is the honest number. The dollar
figures are there for scale.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..config import CFG
from ..providers.base import SOL_QUOTE_MINTS, WRAPPED_NATIVE

log = logging.getLogger("memehunter.traders")

Progress = Callable[[str], Awaitable[None]]

WSOL = "So11111111111111111111111111111111111111112"
NATIVE = "__native__"
# Stablecoins are worth a dollar; everything else needs a price.
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",              # USDC, Solana
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",              # USDT, Solana
}
STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDC.E", "USDBC"}

SOL_CONCURRENCY = 4


async def _noop(_: str) -> None:
    return None


@dataclass
class Trader:
    """One wallet's record on one coin."""
    wallet: str
    chain: str
    spent_quote: float = 0.0        # quote asset paid in, e.g. SOL or ETH
    received_quote: float = 0.0     # quote asset taken out
    bought_tokens: float = 0.0
    sold_tokens: float = 0.0
    buy_txns: int = 0
    sell_txns: int = 0
    quote_symbol: str = ""
    quote_price_usd: float = 0.0

    @property
    def spent_usd(self) -> float:
        return self.spent_quote * self.quote_price_usd

    @property
    def received_usd(self) -> float:
        return self.received_quote * self.quote_price_usd

    @property
    def pnl_usd(self) -> float:
        """Realised only, which is what a top-traders table shows. A wallet
        still holding reads as a loss until it sells, so `holding` says so."""
        return self.received_usd - self.spent_usd

    @property
    def multiple(self) -> float:
        """Taken out per unit put in, in the quote asset.

        Computed before any dollar conversion, so it does not inherit the
        error in pricing an old trade at today's rate.
        """
        if self.spent_quote <= 0:
            return 0.0
        return self.received_quote / self.spent_quote

    @property
    def held_tokens(self) -> float:
        return max(self.bought_tokens - self.sold_tokens, 0.0)

    @property
    def holding(self) -> bool:
        """Still holding more than dust of what it bought."""
        return self.bought_tokens > 0 and self.held_tokens > 0.01 * self.bought_tokens


@dataclass
class TraderReport:
    token: str
    chain: str
    traders: list[Trader] = field(default_factory=list)
    scanned: int = 0                # wallets seen
    since_ts: int = 0               # when the pair appeared, if known
    partial: bool = False           # history longer than the scan window
    cohort_only: bool = False       # EVM: early buyers rather than everyone
    error: str = ""


def rank(traders: list[Trader], limit: int, min_spend_usd: float) -> list[Trader]:
    """Small in, large out, best first.

    Ranked on the multiple rather than the profit, because the profit alone
    just finds whoever traded biggest. A floor on the entry keeps out the
    wallet that spent a thousandth of a cent and shows a five-figure multiple.
    """
    real = [t for t in traders
            if t.spent_usd >= min_spend_usd and t.received_quote > 0]
    real.sort(key=lambda t: (t.multiple, t.pnl_usd), reverse=True)
    return real[:limit]


# --------------------------------------------------------------------- solana
def _owner_deltas(tx: dict) -> dict[str, dict[str, float]]:
    """Per owner, how every token balance and their native SOL changed."""
    meta = tx.get("meta") or {}
    out: dict[str, dict[str, float]] = {}

    for side, sign in (("preTokenBalances", -1.0), ("postTokenBalances", 1.0)):
        for balance in meta.get(side) or []:
            owner, mint = balance.get("owner"), balance.get("mint")
            if not owner or not mint:
                continue
            amount = float((balance.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            out.setdefault(owner, {})[mint] = out.setdefault(owner, {}).get(mint, 0.0) + sign * amount

    # A bonding-curve buy pays in bare SOL, which never appears as a token
    # balance. Without this every pump.fun entry would look free.
    keys = [k.get("pubkey") if isinstance(k, dict) else k
            for k in ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []]
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    for i, key in enumerate(keys):
        if key and i < len(pre) and i < len(post):
            lamports = (post[i] - pre[i]) / 1e9
            if abs(lamports) > 1e-9:
                out.setdefault(key, {})[NATIVE] = out.setdefault(key, {}).get(NATIVE, 0.0) + lamports
    return out


def _quote_delta(moves: dict[str, float]) -> tuple[float, float]:
    """(quote asset moved, stablecoin dollars moved) for one owner."""
    quote = moves.get(NATIVE, 0.0) + moves.get(WSOL, 0.0)
    stable = sum(v for m, v in moves.items() if m in STABLES)
    return quote, stable


async def _solana_traders(analyzer, token: str, report: TraderReport,
                          progress: Progress) -> None:
    sol = analyzer.sol
    seen_signatures: set[str] = set()
    transactions: list[dict] = []
    pages = max(1, CFG.trader_scan_pages)

    async def walk(order: str) -> bool:
        """Page through the coin's history. True when it ran out of history."""
        page_token = None
        for _ in range(pages):
            options: dict[str, Any] = {
                "sortOrder": order, "limit": 100, "transactionDetails": "full",
                "encoding": "jsonParsed", "maxSupportedTransactionVersion": 1,
            }
            if page_token:
                options["paginationToken"] = page_token
            result = await sol.rpc("getTransactionsForAddress", [token, options])
            if not isinstance(result, dict):
                return True
            batch = result.get("data") or []
            for tx in batch:
                signature = ((tx.get("transaction") or {}).get("signatures") or [""])[0]
                if signature and signature not in seen_signatures:
                    seen_signatures.add(signature)
                    transactions.append(tx)
            await progress(f"Reading {token[:6]}... {len(transactions)} transactions so far")
            page_token = result.get("paginationToken")
            if not page_token or len(batch) < 100:
                return True
        return False

    # Both ends: the entries are at the start of a coin's life and the exits
    # are at the end. Reading only one end would price half of every trade.
    finished_start = await walk("asc")
    finished_end = await walk("desc")
    report.partial = not (finished_start and finished_end)

    by_wallet: dict[str, Trader] = {}
    for tx in transactions:
        if (tx.get("meta") or {}).get("err"):
            continue
        for owner, moves in _owner_deltas(tx).items():
            token_delta = moves.get(token, 0.0)
            if token_delta == 0:
                continue
            quote, stable = _quote_delta(moves)
            trader = by_wallet.setdefault(owner, Trader(wallet=owner, chain="solana"))
            if token_delta > 0 and (quote < 0 or stable < 0):
                trader.bought_tokens += token_delta
                trader.spent_quote += max(-quote, 0.0)
                trader.buy_txns += 1
            elif token_delta < 0 and (quote > 0 or stable > 0):
                trader.sold_tokens += -token_delta
                trader.received_quote += max(quote, 0.0)
                trader.sell_txns += 1

    # Pools, bonding curves and vaults trade constantly and are not people.
    wallets = await sol._only_wallets(list(by_wallet))
    prices = await analyzer.dex.markets([WSOL])
    sol_price = prices.get(WSOL).price_usd if prices.get(WSOL) else 0.0

    for address, trader in by_wallet.items():
        if address not in wallets:
            continue
        trader.quote_symbol = "SOL"
        trader.quote_price_usd = sol_price
        report.traders.append(trader)
    report.scanned = len(report.traders)


# ------------------------------------------------------------------------ evm
def _amount(total: dict | None) -> float:
    total = total or {}
    try:
        return int(total.get("value") or 0) / (10 ** int(total.get("decimals") or 18))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _wei(value: Any) -> float:
    try:
        return int(value or 0) / 1e18
    except (TypeError, ValueError):
        return 0.0


async def _v2_items(provider, url: str, params: dict, pages: int) -> list[dict]:
    """Every row across a few pages of a Blockscout listing."""
    items: list[dict] = []
    query = dict(params)
    for _ in range(max(1, pages)):
        data = await provider._v2_get(url, query)
        if not isinstance(data, dict):
            break
        items.extend(data.get("items") or [])
        nxt = data.get("next_page_params")
        if not nxt:
            break
        query = {**params, **nxt}
    return items


async def _evm_wallet(provider, v2: str, wallet: str, token: str, chain: str,
                      quote_symbol: str, quote_price: float) -> Trader:
    """One wallet's trades of one token, valued in the chain's gas asset.

    An EVM swap does not pay the seller in an ERC-20 the way a Solana one
    does. On Base the proceeds arrive as bare ETH through an internal call, so
    the token transfer and the money are in different listings and have to be
    joined by transaction. Three listings cover it: what the token did, what
    the wallet spent, and what came back.
    """
    trader = Trader(wallet=wallet, chain=chain,
                    quote_symbol=quote_symbol, quote_price_usd=quote_price)
    address = wallet.lower()
    native = (WRAPPED_NATIVE.get(chain) or "").lower()
    pages = CFG.trader_pages

    moves = await _v2_items(provider, f"{v2}/addresses/{wallet}/token-transfers",
                            {"type": "ERC-20", "token": token}, pages)
    if not moves:
        return trader

    token_in: dict[str, float] = {}
    token_out: dict[str, float] = {}
    for move in moves:
        digest = move.get("transaction_hash")
        if not digest:
            continue
        amount = _amount(move.get("total"))
        if ((move.get("to") or {}).get("hash") or "").lower() == address:
            token_in[digest] = token_in.get(digest, 0.0) + amount
        elif ((move.get("from") or {}).get("hash") or "").lower() == address:
            token_out[digest] = token_out.get(digest, 0.0) + amount
    traded = set(token_in) | set(token_out)

    spent: dict[str, float] = {}
    received: dict[str, float] = {}

    # What the wallet sent: the gas asset attached to its own transaction.
    for tx in await _v2_items(provider, f"{v2}/addresses/{wallet}/transactions",
                              {"filter": "from"}, pages):
        digest = tx.get("hash")
        if digest in traded:
            spent[digest] = spent.get(digest, 0.0) + _wei(tx.get("value"))

    # What came back: an internal call paying the wallet in the gas asset.
    for internal in await _v2_items(
            provider, f"{v2}/addresses/{wallet}/internal-transactions", {}, pages):
        digest = internal.get("transaction_hash")
        if digest in traded and ((internal.get("to") or {}).get("hash") or "").lower() == address:
            received[digest] = received.get(digest, 0.0) + _wei(internal.get("value"))

    # Some routers settle in wrapped gas or a stablecoin instead, which does
    # reach the wallet as an ordinary transfer.
    for other in await _v2_items(provider, f"{v2}/addresses/{wallet}/token-transfers",
                                 {"type": "ERC-20"}, pages):
        digest = other.get("transaction_hash")
        if digest not in traded:
            continue
        contract = (((other.get("token") or {}).get("address_hash")
                     or (other.get("token") or {}).get("address")) or "").lower()
        symbol = ((other.get("token") or {}).get("symbol") or "").upper()
        if contract == native:
            value = _amount(other.get("total"))
        elif symbol in STABLE_SYMBOLS and quote_price:
            value = _amount(other.get("total")) / quote_price
        else:
            continue
        if ((other.get("to") or {}).get("hash") or "").lower() == address:
            received[digest] = received.get(digest, 0.0) + value
        elif ((other.get("from") or {}).get("hash") or "").lower() == address:
            spent[digest] = spent.get(digest, 0.0) + value

    for digest in traded:
        gained = token_in.get(digest, 0.0) - token_out.get(digest, 0.0)
        paid, took = spent.get(digest, 0.0), received.get(digest, 0.0)
        if gained > 0 and paid > 0:
            trader.bought_tokens += gained
            trader.spent_quote += paid
            trader.buy_txns += 1
        elif gained < 0 and took > 0:
            trader.sold_tokens += -gained
            trader.received_quote += took
            trader.sell_txns += 1
    return trader


async def _evm_traders(analyzer, token: str, chain: str, report: TraderReport,
                       progress: Progress) -> None:
    provider = analyzer.evm(chain)
    # Blockscout regardless of whether Etherscan has already handed this chain
    # over. Valuing a trade needs the internal calls of a transaction, which
    # the Etherscan free plan does not serve at any price.
    v2 = provider.fallback.get("v2")
    if not v2:
        report.error = (
            f"Top traders on {chain} needs the public Blockscout API, which this "
            f"chain does not have here. Solana and the Blockscout chains "
            f"(ethereum, base, arbitrum, polygon) are supported."
        )
        return

    report.cohort_only = True
    buyers = await provider.early_buyers(token, CFG.trader_cohort,
                                         since_ts=report.since_ts)
    if not buyers:
        report.error = provider.last_error or "No early buyers found for that token."
        return

    native = (WRAPPED_NATIVE.get(chain) or "").lower()
    markets = await analyzer.dex.markets([native] if native else [])
    market = markets.get(native) if native else None
    quote_price = market.price_usd if market else 0.0
    quote_symbol = (market.symbol if market else "") or "ETH"

    semaphore = asyncio.Semaphore(3)
    done = 0

    async def one(address: str) -> Trader | None:
        nonlocal done
        async with semaphore:
            try:
                trader = await _evm_wallet(provider, v2, address, token, chain,
                                           quote_symbol, quote_price)
            except Exception:
                log.exception("pricing %s failed", address)
                return None
        done += 1
        if done % 3 == 0:
            await progress(f"Priced {done} of {len(buyers)} wallets...")
        return trader if (trader.buy_txns or trader.sell_txns) else None

    for result in await asyncio.gather(*(one(b.address) for b in buyers),
                                       return_exceptions=True):
        if isinstance(result, Trader):
            report.traders.append(result)
    report.scanned = len(buyers)


# ---------------------------------------------------------------------- entry
async def top_traders(analyzer, token: str, chain: str, since_ts: int = 0,
                      progress: Progress = _noop) -> TraderReport:
    """Every trader we can see on this coin, ranked by what they made of it."""
    report = TraderReport(token=token, chain=chain)
    report.since_ts = since_ts
    try:
        if chain == "solana":
            await _solana_traders(analyzer, token, report, progress)
        else:
            await _evm_traders(analyzer, token, chain, report, progress)
    except Exception as exc:
        log.exception("top traders failed for %s on %s", token, chain)
        report.error = f"The {chain} scan failed: {exc}"
    return report
