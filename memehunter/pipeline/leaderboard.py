"""Ranking wallets by what their calls were actually worth to you.

A wallet's own PnL is the wrong measure for copy trading. It is measured on
entries you could not take at prices you did not get, and it is dominated by
positions opened long before you started watching. What matters is narrower and
more honest: since you started following this wallet, how did the coins it
entered behave afterwards?

So every entry alert records the price at the moment it fired, the price is
re-checked on a timer, and a wallet is graded on that record alone.

Two deliberate choices in the maths:

* Peak, not last. A coin that went 4x and came back to flat was a good call
  badly managed, and grading it as a loss would tell you to stop following a
  wallet that keeps handing you 4x. Both numbers are shown; the ranking uses
  peak, and `avg_last` is there to expose a wallet whose calls always round-trip.

* Small samples are shrunk toward a base rate. A wallet that is 1-for-1 is not
  better than one that is 12-for-20, and an unshrunk hit rate would rank it top
  forever. Confidence rises with sample size instead of being asserted.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..config import CFG


@dataclass
class WalletCard:
    wallet: str
    chain: str = "solana"
    label: str | None = None
    signals: int = 0
    hits: int = 0
    open_count: int = 0
    peaks: list[float] = field(default_factory=list)
    lasts: list[float] = field(default_factory=list)
    best_peak: float = 1.0
    best_symbol: str = ""
    worst_last: float = 1.0
    worst_symbol: str = ""
    last_signal_ts: int = 0

    # ---------------------------------------------------------------- measures
    @property
    def hit_rate(self) -> float:
        return self.hits / self.signals if self.signals else 0.0

    @property
    def avg_peak(self) -> float:
        return statistics.fmean(self.peaks) if self.peaks else 1.0

    @property
    def median_peak(self) -> float:
        return statistics.median(self.peaks) if self.peaks else 1.0

    @property
    def avg_last(self) -> float:
        """Where the calls stand now. Well below avg_peak means this wallet's
        coins round-trip, so its edge depends on you taking profit yourself."""
        return statistics.fmean(self.lasts) if self.lasts else 1.0

    @property
    def round_trip(self) -> bool:
        return bool(self.peaks) and self.avg_peak >= 1.5 and self.avg_last < 1.05

    @property
    def confidence(self) -> float:
        """Hit rate shrunk toward the base rate by sample size.

        With no history this equals the prior; it converges on the raw hit rate
        only once the wallet has given you enough calls to mean something.
        """
        w = CFG.leaderboard_prior_weight
        return (self.hits + w * CFG.leaderboard_prior_rate) / (self.signals + w)

    @property
    def rank_score(self) -> float:
        """What the board sorts on: shrunk hit rate, tilted by how big the
        winners were. A wallet that hits often but small ranks below one that
        hits as often and occasionally hands you a 10x."""
        upside = min(self.avg_peak, 10.0) / 10.0
        return self.confidence * 0.75 + upside * 0.25

    @property
    def grade(self) -> str:
        if self.signals < 3:
            return "NEW"
        c = self.confidence
        if c >= 0.55:
            return "A"
        if c >= 0.40:
            return "B"
        if c >= 0.28:
            return "C"
        return "D"

    @property
    def verdict(self) -> str:
        if self.signals < 3:
            return "too few calls to judge"
        # Checked before the grade: a wallet can hit often and still hand back
        # every gain, and "worth following" would bury the part you must act on.
        if self.round_trip:
            return "good entries, you must exit yourself"
        if self.grade == "A":
            return "worth following"
        if self.grade == "B":
            return "decent, size small"
        if self.grade == "C":
            return "coin flip so far"
        return "losing you money"


def _multiple(entry: float, price: float) -> float:
    return price / entry if entry > 0 and price > 0 else 1.0


def build_scorecards(
    rows: Iterable[Any],
    labels: dict[str, str] | None = None,
    win_multiple: float | None = None,
) -> list[WalletCard]:
    """Fold outcome rows into one card per wallet, best first.

    `rows` are alert_outcomes records (sqlite Rows or plain dicts). Only entry
    alerts are graded -- an exit alert has no "did it go up" to measure.
    """
    win = win_multiple if win_multiple is not None else CFG.outcome_win_multiple
    labels = labels or {}
    cards: dict[str, WalletCard] = {}

    for row in rows:
        get = row.get if isinstance(row, dict) else row.__getitem__
        try:
            kind = get("kind")
            if kind not in ("BUY", "ADD"):
                continue
            wallet = get("wallet")
            entry = float(get("entry_price") or 0)
            if entry <= 0:
                continue
            peak = _multiple(entry, float(get("peak_price") or 0))
            last = _multiple(entry, float(get("last_price") or 0))
            symbol = get("symbol") or get("token")[:6]
            opened = int(get("opened_at") or 0)
            closed = bool(get("closed"))
            chain = get("chain") or "solana"
        except (KeyError, IndexError, TypeError, ValueError):
            continue

        card = cards.get(wallet)
        if card is None:
            card = cards[wallet] = WalletCard(
                wallet=wallet, chain=chain, label=labels.get(wallet))

        card.signals += 1
        card.peaks.append(peak)
        card.lasts.append(last)
        if not closed:
            card.open_count += 1
        if peak >= win:
            card.hits += 1
        if peak > card.best_peak:
            card.best_peak, card.best_symbol = peak, symbol
        if last < card.worst_last:
            card.worst_last, card.worst_symbol = last, symbol
        card.last_signal_ts = max(card.last_signal_ts, opened)

    ranked = sorted(cards.values(), key=lambda c: (c.rank_score, c.signals),
                    reverse=True)
    return ranked


def summarise(cards: Sequence[WalletCard], win_multiple: float | None = None) -> dict:
    """Portfolio-level read across every wallet, for the header of the board."""
    win = win_multiple if win_multiple is not None else CFG.outcome_win_multiple
    signals = sum(c.signals for c in cards)
    hits = sum(c.hits for c in cards)
    peaks = [p for c in cards for p in c.peaks]
    return {
        "wallets": len(cards),
        "signals": signals,
        "hits": hits,
        "hit_rate": (hits / signals) if signals else 0.0,
        "avg_peak": statistics.fmean(peaks) if peaks else 1.0,
        "median_peak": statistics.median(peaks) if peaks else 1.0,
        "win_multiple": win,
        "graded": [c for c in cards if c.signals >= 3],
    }


def stale_wallets(cards: Sequence[WalletCard], quiet_days: int = 21) -> list[WalletCard]:
    """Wallets that have gone silent. Step 3 of the method is 'drop the dead
    ones', and a wallet you tracked a month ago can die like any other."""
    cutoff = time.time() - quiet_days * 86400
    return [c for c in cards if c.last_signal_ts and c.last_signal_ts < cutoff]


def underperformers(cards: Sequence[WalletCard], min_signals: int = 5) -> list[WalletCard]:
    """Graded wallets that are costing you money. Surfaced so the tracking list
    gets pruned on evidence rather than on how it felt."""
    return [c for c in cards
            if c.signals >= min_signals and c.grade == "D"]
