"""Checks for exit detection and position sizing. Run via selftest.py."""
from __future__ import annotations

from memehunter.pipeline.events import (ADD, BUY, EXIT, TRIM, diff_positions,
                                        looks_like_read_failure)
from memehunter.pipeline.portfolio import Portfolio, Position, size_for_you


def _kinds(events) -> dict[str, str]:
    return {e.token: e.kind for e in events}


def run(check) -> None:
    print("exit detection")
    prev = {"A": {"amount": 1000.0, "peak": 1000.0}}

    check("untouched position is silent", diff_positions(prev, {"A": 1000.0}) == [])
    check("noise below the trim threshold is silent",
          diff_positions(prev, {"A": 950.0}) == [])

    sold_half = diff_positions(prev, {"A": 500.0})
    check("halving the position is a trim", _kinds(sold_half) == {"A": TRIM})
    check("trim reports the percentage sold",
          abs(sold_half[0].change_pct + 50) < 0.01, str(sold_half[0].change_pct))

    check("balance going to zero is an exit",
          _kinds(diff_positions(prev, {})) == {"A": EXIT})
    check("selling down to dust is an exit",
          _kinds(diff_positions(prev, {"A": 10.0})) == {"A": EXIT})
    check("dust already sitting there is not re-reported",
          diff_positions({"A": {"amount": 10.0, "peak": 1000.0}}, {"A": 10.0}) == [])

    check("a token never seen before is a buy",
          _kinds(diff_positions({}, {"B": 500.0})) == {"B": BUY})
    check("buying back in after a full exit is a new buy",
          _kinds(diff_positions({"A": {"amount": 0.0, "peak": 1000.0}},
                                {"A": 300.0})) == {"A": BUY})
    check("topping up is an add",
          _kinds(diff_positions(prev, {"A": 2000.0})) == {"A": ADD})

    # Peak matters: a position bought, doubled, then cut back to its original size
    # has really been halved from the top, and that is what a follower cares about.
    scaled = diff_positions({"A": {"amount": 2000.0, "peak": 2000.0}}, {"A": 1000.0})
    check("trim measured against peak, not entry",
          abs(scaled[0].sold_share_of_peak - 0.5) < 0.01,
          str(scaled[0].sold_share_of_peak))

    mixed = diff_positions(
        {"A": {"amount": 1000.0, "peak": 1000.0},
         "B": {"amount": 500.0, "peak": 500.0}},
        {"A": 0.0, "B": 500.0, "C": 90.0})
    check("exits sort ahead of buys", mixed[0].kind == EXIT, str(_kinds(mixed)))
    check("unchanged position stays out of a mixed diff", "B" not in _kinds(mixed))
    check("new token in a mixed diff is a buy", _kinds(mixed).get("C") == BUY)

    check("thresholds are tunable",
          _kinds(diff_positions(prev, {"A": 950.0}, trim_pct=1)) == {"A": TRIM})

    print("read-failure guard")
    many = {t: {"amount": 100.0, "peak": 100.0} for t in "ABCD"}
    check("empty read on a full wallet is treated as a failure",
          looks_like_read_failure(many, {}))
    check("genuine single exit is not suppressed",
          not looks_like_read_failure({"A": {"amount": 1.0, "peak": 1.0}}, {}))
    check("a real read is never suppressed",
          not looks_like_read_failure(many, {"A": 100.0}))

    print("position sizing")
    pf = Portfolio(wallet="w", chain="solana", positions=[
        Position(token="A", symbol="A", amount=1.0, usd=80_000),
        Position(token="B", symbol="B", amount=1.0, usd=320_000),
    ], native_usd=100_000)
    check("book totals tokens plus native", pf.total_usd == 500_000, str(pf.total_usd))
    check("share of book is correct", abs(pf.share_of_book("A") - 0.16) < 1e-9)
    check("largest position sorts first", pf.top(1)[0].token == "B")

    sizing = size_for_you(pf, "A", bankroll=2_000)
    check("their dollar size reported", sizing.their_usd == 80_000)
    check("conviction scales to your bankroll", abs(sizing.your_usd - 320) < 0.01,
          str(sizing.your_usd))
    check("a big account does not become a big instruction",
          sizing.your_usd < sizing.their_usd)

    no_bankroll = size_for_you(pf, "A", bankroll=0)
    check("unset bankroll yields no size", no_bankroll.your_usd == 0)
    check("share still known without a bankroll", no_bankroll.their_share > 0)

    heavy = Portfolio(wallet="w", chain="solana", positions=[
        Position(token="A", symbol="A", amount=1.0, usd=90_000),
        Position(token="B", symbol="B", amount=1.0, usd=10_000),
    ])
    check("all-in conviction is called out",
          "gambling" in size_for_you(heavy, "A", 1_000).note)

    lottery = Portfolio(wallet="w", chain="solana", positions=[
        Position(token="A", symbol="A", amount=1.0, usd=50),
        Position(token="B", symbol="B", amount=1.0, usd=100_000),
    ])
    check("token position is flagged as a token position",
          "lottery ticket" in size_for_you(lottery, "A", 1_000).note)

    empty = size_for_you(Portfolio(wallet="w", chain="solana"), "A", 1_000)
    check("unpriceable book degrades safely",
          empty.your_usd == 0 and not empty.known)

    unknown = size_for_you(pf, "ZZZ", 2_000)
    check("token they do not hold sizes to zero", unknown.your_usd == 0)
