"""Entry point: python main.py"""
from __future__ import annotations

import logging
import sys

from memehunter.bot import build_app
from memehunter.config import CFG


def main() -> int:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    missing = CFG.missing_required()
    if missing:
        print("Missing required settings in .env: " + ", ".join(missing))
        print("Copy .env.example to .env and fill them in.")
        return 1

    if not (CFG.helius_key or CFG.solscan_key):
        logging.warning(
            "No Helius or Solscan key set - falling back to the public Solana RPC. "
            "It works, but early-buyer lookups will be slow and may rate limit."
        )
    if not CFG.etherscan_key:
        logging.warning("No Etherscan key set - EVM chains are disabled.")

    build_app().run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
