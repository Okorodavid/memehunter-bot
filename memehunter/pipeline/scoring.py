"""Step 7: the scoring system. Ten points, seven components, nothing hidden.

Every component returns points out of a fixed maximum plus a one-line reason, so a
score can always be read backwards into the facts that produced it. Tune the bands
here rather than anywhere else -- this is the only file that encodes opinion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..providers.base import TokenMarket


@dataclass
class Component:
    label: str
    points: float
    maximum: float
    note: str


@dataclass
class Score:
    total: float = 0.0
    components: list[Component] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)

    def add(self, label: str, points: float, maximum: float, note: str) -> None:
        self.components.append(Component(label, round(points, 2), maximum, note))
        self.total = round(self.total + points, 2)

    @property
    def verdict(self) -> str:
        if self.flags:
            return "AVOID"
        if self.total >= 9.0:
            return "BUY-GRADE"
        if self.total >= 7.0:
            return "WATCH"
        return "PASS"


def _band(value: float, bands: list[tuple[float, float, str]], default: tuple[float, str]):
    """bands: (upper_bound_exclusive, points, note), checked in order."""
    for upper, pts, note in bands:
        if value < upper:
            return pts, note
    return default


def score_token(
    market: TokenMarket | None,
    overlap_wallets: int,
    safety: dict[str, Any] | None,
    chain: str,
) -> Score:
    s = Score()

    if market is None:
        s.flags.append("no DEX pair found - unlisted, dead, or wrong chain")
        s.add("Market data", 0, 10, "no pair on DexScreener")
        return s

    # 1. Smart-money overlap (0-3) -- step 6, the core signal of the whole method.
    overlap_points = {0: 0.0, 1: 1.0, 2: 2.0, 3: 2.6, 4: 2.9}.get(overlap_wallets, 3.0)
    s.add("Smart-money overlap", overlap_points, 3.0,
          f"{overlap_wallets} tracked wallet(s) hold it")

    # 2. Liquidity (0-1.5) -- deep enough to exit, small enough to still run.
    liq = market.liquidity_usd
    pts, note = _band(liq, [
        (5_000, 0.0, "under $5k, you cannot exit"),
        (20_000, 0.6, "thin"),
        (150_000, 1.5, "sweet spot for asymmetric upside"),
        (1_000_000, 1.2, "healthy but the easy multiple is gone"),
        (5_000_000, 0.7, "large, 100x needs a new narrative"),
    ], (0.3, "very large, wrong end of the curve"))
    s.add("Liquidity", pts, 1.5, f"${liq:,.0f} - {note}")

    # 3. Age (0-1) -- old enough not to be a launch snipe, young enough to matter.
    age = market.age_days
    pts, note = _band(age, [
        (0.04, 0.3, "under an hour old, coin-flip territory"),
        (1, 0.8, "less than a day old"),
        (7, 1.0, "days old, the window this method targets"),
        (30, 0.8, "weeks old"),
        (90, 0.5, "months old"),
    ], (0.2, "old, the cycle has moved on"))
    s.add("Age", pts, 1.0, f"{age:.1f} days - {note}")

    # 4. Volume / liquidity turnover (0-1.5) -- real interest vs. wash trading.
    ratio = market.volume_24h / liq if liq > 0 else 0.0
    pts, note = _band(ratio, [
        (0.2, 0.2, "almost no turnover"),
        (0.5, 0.7, "quiet"),
        (5, 1.5, "active and organic"),
        (20, 1.0, "very hot, late-entry risk"),
    ], (0.3, "turnover looks washed"))
    s.add("Volume/liquidity", pts, 1.5, f"{ratio:.1f}x - {note}")

    # 5. Buy pressure (0-1) -- who is winning the tape right now.
    total_tx = market.buys_24h + market.sells_24h
    share = market.buys_24h / total_tx if total_tx else 0.0
    if total_tx < 50:
        pts, note = 0.2, "too few trades to read"
    else:
        pts, note = _band(share, [
            (0.45, 0.1, "sellers in control"),
            (0.50, 0.4, "slight distribution"),
            (0.60, 0.7, "balanced"),
        ], (1.0, "buyers in control"))
    s.add("Buy pressure", pts, 1.0, f"{share:.0%} buys of {total_tx} txs - {note}")

    # 6. Contract safety (0-1.5) -- chain-specific, the cheap rug checks.
    safety = safety or {}
    if chain == "solana":
        pts, bits = 0.0, []
        if safety.get("mint_authority") in (None, ""):
            pts += 0.6
            bits.append("mint revoked")
        else:
            s.flags.append("mint authority still live - supply can be inflated")
            bits.append("MINT AUTHORITY LIVE")
        if safety.get("freeze_authority") in (None, ""):
            pts += 0.5
            bits.append("freeze revoked")
        else:
            s.flags.append("freeze authority still live - your tokens can be frozen")
            bits.append("FREEZE AUTHORITY LIVE")
        conc = float(safety.get("top10_share_ex_largest") or 0)
        if conc and conc < 0.25:
            pts += 0.4
            bits.append(f"top10 ex-LP {conc:.0%}")
        elif conc:
            bits.append(f"top10 ex-LP {conc:.0%} concentrated")
            if conc > 0.5:
                s.flags.append(f"top 10 non-LP holders own {conc:.0%} of supply")
        s.add("Contract safety", pts, 1.5, ", ".join(bits) or "unknown")
    else:
        pts, bits = 0.5, []
        if safety.get("verified"):
            pts += 0.7
            bits.append("source verified")
        else:
            bits.append("source unverified")
            s.flags.append("contract source is not verified")
        if not safety.get("proxy"):
            pts += 0.3
            bits.append("not a proxy")
        else:
            bits.append("upgradeable proxy")
            s.flags.append("upgradeable proxy - logic can change under you")
        s.add("Contract safety", min(pts, 1.5), 1.5, ", ".join(bits))

    # 7. Momentum (0-0.5) -- is it moving now, on both horizons.
    h6, h24 = market.price_change_6h, market.price_change_24h
    if h6 > 0 and h24 > 0:
        pts, note = 0.5, "up on 6h and 24h"
    elif h6 > 0:
        pts, note = 0.35, "turning up on 6h"
    elif h24 > 0:
        pts, note = 0.2, "24h green, 6h fading"
    else:
        pts, note = 0.0, "red on both"
    s.add("Momentum", pts, 0.5, f"6h {h6:+.0f}% / 24h {h24:+.0f}% - {note}")

    # Hard disqualifiers override any score.
    if liq < 3_000:
        s.flags.append("liquidity below $3k - effectively untradeable")
    if total_tx and share < 0.3:
        s.flags.append("heavy net selling in the last 24h")

    s.total = round(min(s.total, 10.0), 2)
    return s
