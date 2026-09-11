"""Central configuration, loaded once from the environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_user_ids: frozenset[int] = field(
        default_factory=lambda: frozenset(
            int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
        )
    )

    helius_key: str = os.getenv("HELIUS_API_KEY", "")
    solscan_key: str = os.getenv("SOLSCAN_API_KEY", "")
    etherscan_key: str = os.getenv("ETHERSCAN_API_KEY", "")

    early_buyer_count: int = _int("EARLY_BUYER_COUNT", 20)
    active_window_days: int = _int("ACTIVE_WINDOW_DAYS", 30)
    recent_buy_window_days: int = _int("RECENT_BUY_WINDOW_DAYS", 30)
    overlap_min_wallets: int = _int("OVERLAP_MIN_WALLETS", 3)
    score_buy_threshold: float = _float("SCORE_BUY_THRESHOLD", 9.0)

    # Bot-detection knobs (step 4).
    bot_min_median_gap_sec: float = _float("BOT_MIN_MEDIAN_GAP_SEC", 3)
    bot_max_tx_per_day: float = _float("BOT_MAX_TX_PER_DAY", 250)

    # Exit detection. A position that shrinks by more than trim_pct is a partial
    # sell; one that drops below exit_pct of what it was is treated as fully closed.
    trim_alert_pct: float = _float("TRIM_ALERT_PCT", 15)
    exit_dust_pct: float = _float("EXIT_DUST_PCT", 5)

    # Position sizing. Your account size, used to scale their conviction to you.
    bankroll_usd: float = _float("BANKROLL_USD", 0)
    # Cap on how many token balances get priced per wallet, to bound API cost.
    max_priced_holdings: int = _int("MAX_PRICED_HOLDINGS", 120)

    watcher_interval_min: int = _int("WATCHER_INTERVAL_MIN", 10)
    db_path: str = os.getenv("DB_PATH", "memehunter.db")

    @property
    def solana_rpc(self) -> str:
        explicit = os.getenv("SOLANA_RPC_URL", "").strip()
        if explicit:
            return explicit
        if self.helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}"
        return "https://api.mainnet-beta.solana.com"

    def missing_required(self) -> list[str]:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_user_ids:
            missing.append("ALLOWED_USER_IDS")
        return missing


CFG = Config()
