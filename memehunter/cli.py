"""Terminal harness for the same pipeline, so you can test without Telegram.

    python -m memehunter.cli wallets <token>
    python -m memehunter.cli analyze <token>
    python -m memehunter.cli score   <token>
"""
from __future__ import annotations

import asyncio
import re
import sys

from .pipeline.analyzer import Analyzer
from .render import render_analysis, render_candidate, render_score

TAGS = re.compile(r"<[^>]+>")


def plain(html_text: str) -> str:
    return TAGS.sub("", html_text).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def run(command: str, token: str) -> None:
    az = Analyzer()

    async def progress(text: str) -> None:
        print(f"  ... {text}")

    try:
        if command == "wallets":
            res = await az.find_smart_wallets(token, progress)
            print(f"\nchain={res.chain} early={len(res.early_buyers)} "
                  f"dead={res.dead_count} bots={res.bot_count} "
                  f"survivors={len(res.survivors)}")
            for p in res.survivors:
                print(f"  {p.address}  last {p.days_since_active:.1f}d ago  "
                      f"{p.tx_count_30d} tx/30d  median gap {p.median_gap_sec:.0f}s")
            if res.error:
                print(f"  error: {res.error}")
        elif command == "analyze":
            res = await az.analyze_token(token, progress)
            print("\n" + plain(render_analysis(res, az)))
        elif command == "score":
            cand, _ = await az.score_one(token)
            print("\n" + plain(render_candidate(cand, az)))
            print(plain(render_score(cand.score)))
        else:
            print(__doc__)
    finally:
        await az.aclose()


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    asyncio.run(run(sys.argv[1], sys.argv[2]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
