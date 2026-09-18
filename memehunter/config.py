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


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _chain_list(key: str) -> tuple[str, ...]:
    """Chains to expand an EVM wallet across. Empty env value means all of them."""
    from .providers.base import EVM_CHAINS  # imported late to avoid a cycle
    raw = os.getenv(key, "").strip()
    if not raw:
        return tuple(EVM_CHAINS)
    wanted = [c.strip().lower() for c in raw.replace(";", ",").split(",") if c.strip()]
    return tuple(c for c in wanted if c in EVM_CHAINS) or tuple(EVM_CHAINS)


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

    # Watch an EVM wallet on every EVM chain, not just the one you added it on.
    # The same address is the same person everywhere, so a wallet with a good
    # record on Base is worth watching when it first appears on Arc.
    multichain: bool = _bool("MULTICHAIN", True)
    multichain_chains: tuple[str, ...] = field(
        default_factory=lambda: _chain_list("MULTICHAIN_CHAINS"))
    # Chains where the wallet shows no history get parked after this many
    # consecutive empty reads, and retried this often.
    multichain_dormant_after: int = _int("MULTICHAIN_DORMANT_AFTER", 3)
    multichain_reprobe_hours: float = _float("MULTICHAIN_REPROBE_HOURS", 12)
    multichain_probes_per_cycle: int = _int("MULTICHAIN_PROBES_PER_CYCLE", 12)

    # Automatic scanning. /scan runs steps 5-7 on demand; autoscan runs the same
    # work on a timer and only speaks when something actually changed, so a quiet
    # market stays quiet instead of sending you an identical digest every cycle.
    autoscan_hours: float = _float("AUTOSCAN_INTERVAL_HOURS", 6)
    autoscan_min_score: float = _float("AUTOSCAN_MIN_SCORE", 7.0)

    # Automatic harvesting: work through the queue of past runners one coin at a
    # time, so a long queue spreads its API cost over days instead of one burst.
    autoharvest_hours: float = _float("AUTOHARVEST_INTERVAL_HOURS", 12)
    autoharvest_max_wallets: int = _int("AUTOHARVEST_MAX_WALLETS", 10)

    # Outcome tracking: what happened to each coin after an alert fired. This is
    # what the wallet leaderboard is built from.
    outcome_check_min: int = _int("OUTCOME_CHECK_MIN", 20)
    outcome_track_hours: float = _float("OUTCOME_TRACK_HOURS", 72)
    outcome_win_multiple: float = _float("OUTCOME_WIN_MULTIPLE", 1.5)
    # Small samples are shrunk toward this base rate so a wallet that is 1-for-1
    # cannot outrank one that is 12-for-20.
    leaderboard_prior_rate: float = _float("LEADERBOARD_PRIOR_RATE", 0.25)
    leaderboard_prior_weight: float = _float("LEADERBOARD_PRIOR_WEIGHT", 4)

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
