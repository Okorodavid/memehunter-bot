"""Position sizing: what a wallet is actually risking, and what that means for you.

Knowing that a wallet bought a coin tells you very little on its own. A $500 punt and
a $500,000 conviction bet look identical in a transfer log. This module prices the
whole book so a new position can be read as a percentage of it, then scales that
percentage to your own bankroll.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..config import CFG
from ..providers.base import WRAPPED_NATIVE, Holding


@dataclass
class Position:
    token: str
    symbol: str
    amount: float
    usd: float = 0.0
    price: float = 0.0

    @property
    def priced(self) -> bool:
        return self.usd > 0


@dataclass
class Portfolio:
    wallet: str
    chain: str
    positions: list[Position] = field(default_factory=list)
    native_usd: float = 0.0
    native_symbol: str = ""
    native_amount: float = 0.0
    unpriced: int = 0
    truncated: bool = False

    @property
    def token_usd(self) -> float:
        return sum(p.usd for p in self.positions)

    @property
    def total_usd(self) -> float:
        return self.token_usd + self.native_usd

    def top(self, n: int = 8) -> list[Position]:
        return sorted(self.positions, key=lambda p: p.usd, reverse=True)[:n]

    def find(self, token: str) -> Position | None:
        low = token.lower()
        return next((p for p in self.positions if p.token.lower() == low), None)

    def share_of_book(self, token: str) -> float:
        """What fraction of everything they hold is sitting in this one position."""
        position = self.find(token)
        if not position or self.total_usd <= 0:
            return 0.0
        return position.usd / self.total_usd


@dataclass
class Sizing:
    """Their conviction, translated into your money."""
    their_usd: float
    their_share: float
    portfolio_usd: float
    your_bankroll: float
    your_usd: float
    note: str = ""

    @property
    def known(self) -> bool:
        return self.portfolio_usd > 0


def size_for_you(portfolio: Portfolio, token: str, bankroll: float) -> Sizing:
    """Mirror their conviction, not their ticket size.

    Copying a dollar amount is the mistake: $10k is a rounding error to them and your
    whole account to you. What transfers between accounts of different sizes is the
    *fraction* committed, so that is what gets scaled.
    """
    position = portfolio.find(token)
    their_usd = position.usd if position else 0.0
    share = portfolio.share_of_book(token)
    your_usd = share * bankroll if bankroll > 0 else 0.0

    note = ""
    if portfolio.total_usd <= 0:
        note = "could not price their book, so this is a guess at best"
    elif portfolio.unpriced:
        note = (f"{portfolio.unpriced} of their holdings have no market price and are "
                f"excluded, so their book is at least this big")
    if share > 0.5:
        note = "over half their book in one coin - they are gambling, not allocating"
    elif share > 0.25:
        note = "a quarter of their book in one coin - unusually heavy conviction"
    elif 0 < share < 0.01:
        note = "under 1% of their book - this is a lottery ticket to them, not a bet"

    return Sizing(
        their_usd=their_usd,
        their_share=share,
        portfolio_usd=portfolio.total_usd,
        your_bankroll=bankroll,
        your_usd=your_usd,
        note=note,
    )


async def build_portfolio(analyzer, chain: str, wallet: str,
                          holdings: list[Holding] | None = None) -> Portfolio:
    """Price every token a wallet holds, plus its native balance."""
    provider = analyzer.provider(chain)
    if holdings is None:
        try:
            holdings = await provider.wallet_holdings(wallet)
        except Exception:
            holdings = []

    portfolio = Portfolio(wallet=wallet, chain=chain)
    if len(holdings) > CFG.max_priced_holdings:
        holdings = holdings[:CFG.max_priced_holdings]
        portfolio.truncated = True

    native_mint = WRAPPED_NATIVE.get(chain, "")
    addresses = [h.token for h in holdings]
    if native_mint:
        addresses.append(native_mint)

    try:
        markets = await analyzer.dex.markets(addresses)
    except Exception:
        markets = {}

    for holding in holdings:
        market = markets.get(holding.token) or markets.get(holding.token.lower())
        price = market.price_usd if market else 0.0
        position = Position(
            token=holding.token,
            symbol=(market.symbol if market else holding.symbol) or holding.token[:6],
            amount=holding.amount,
            price=price,
            usd=holding.amount * price,
        )
        if not position.priced:
            portfolio.unpriced += 1
        portfolio.positions.append(position)

    native_market = markets.get(native_mint) if native_mint else None
    if native_market:
        try:
            balance = await provider.native_balance(wallet)
        except Exception:
            balance = 0.0
        portfolio.native_amount = balance
        portfolio.native_symbol = native_market.symbol or "native"
        portfolio.native_usd = balance * native_market.price_usd

    return portfolio
