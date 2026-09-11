"""Turning two balance snapshots into position events.

This is the piece that makes selling visible. Buys and sells are equally public on
chain -- the reason exits feel invisible is that nobody is diffing balances. Compare
what a wallet held last poll against what it holds now and every leg shows up: a new
entry, a top-up, a partial trim, a full exit.

What this still cannot do is make you faster than them. A poll interval means you
learn minutes after the fact, never before. Shorten the interval and you narrow the
gap; you never close it.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..config import CFG

BUY, ADD, TRIM, EXIT = "BUY", "ADD", "TRIM", "EXIT"

# Most urgent first, so a digest leads with people leaving rather than arriving.
PRIORITY = {EXIT: 0, TRIM: 1, BUY: 2, ADD: 3}

HEADLINE = {
    BUY: "NEW POSITION",
    ADD: "ADDING",
    TRIM: "TRIMMING",
    EXIT: "EXITED",
}


@dataclass
class PositionEvent:
    kind: str
    token: str
    before: float
    after: float
    peak: float

    @property
    def change_pct(self) -> float:
        """Signed change against the previous balance."""
        if self.before <= 0:
            return 100.0 if self.after > 0 else 0.0
        return (self.after - self.before) / self.before * 100.0

    @property
    def sold_share_of_peak(self) -> float:
        """How much of the largest this position ever reached has been sold off."""
        if self.peak <= 0:
            return 0.0
        return max(0.0, (self.peak - self.after) / self.peak)

    @property
    def is_exit(self) -> bool:
        return self.kind in (TRIM, EXIT)

    def describe(self) -> str:
        if self.kind == BUY:
            return "opened a new position"
        if self.kind == ADD:
            return f"added {self.change_pct:+.0f}% to an existing position"
        if self.kind == TRIM:
            return (f"sold {abs(self.change_pct):.0f}% of the position "
                    f"({self.sold_share_of_peak:.0%} of its peak is gone)")
        if self.after > 0:
            return f"dumped the position - only {self.sold_share_of_peak:.1%} dust left"
        return "closed the position completely"


def diff_positions(
    previous: dict[str, dict],
    current: dict[str, float],
    trim_pct: float | None = None,
    exit_pct: float | None = None,
) -> list[PositionEvent]:
    """Compare a stored snapshot against fresh balances.

    `previous` maps token -> {"amount", "peak"}; `current` maps token -> balance.
    """
    trim = CFG.trim_alert_pct if trim_pct is None else trim_pct
    dust = CFG.exit_dust_pct if exit_pct is None else exit_pct
    events: list[PositionEvent] = []

    for token in set(previous) | set(current):
        record = previous.get(token) or {}
        before = float(record.get("amount") or 0)
        peak = max(float(record.get("peak") or 0), before)
        after = float(current.get(token) or 0)
        dust_line = peak * dust / 100.0

        if after > 0 and before <= 0:
            events.append(PositionEvent(BUY, token, before, after, max(peak, after)))
            continue
        if before > 0 and after <= 0:
            events.append(PositionEvent(EXIT, token, before, after, peak))
            continue
        if before <= 0 and after <= 0:
            continue

        change = (after - before) / before * 100.0
        # Crossing into dust this poll is an exit; sitting in dust already is not.
        if after <= dust_line < before:
            events.append(PositionEvent(EXIT, token, before, after, peak))
        elif change <= -trim:
            events.append(PositionEvent(TRIM, token, before, after, peak))
        elif change >= trim:
            events.append(PositionEvent(ADD, token, before, after, max(peak, after)))

    events.sort(key=lambda e: (PRIORITY.get(e.kind, 9), -abs(e.change_pct)))
    return events


def looks_like_read_failure(previous: dict[str, dict], current: dict[str, float]) -> bool:
    """Guard against alerting a fake mass exit when a balance lookup half-failed.

    An empty result for a wallet that held several positions a moment ago is far more
    likely to be a flaky RPC than a trader liquidating everything at once.
    """
    held_before = sum(1 for r in previous.values() if float(r.get("amount") or 0) > 0)
    return held_before >= 3 and not current
