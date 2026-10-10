"""Central configuration, loaded once from the environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

from .dns import DOH_RESOLVERS

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


def _csv(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """A comma-separated env value, or the default when it is blank."""
    raw = os.getenv(key, "").strip()
    if not raw:
        return default
    return tuple(v.strip() for v in raw.replace(";", ",").split(",") if v.strip()) or default


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
    # Every credential is stripped. python-dotenv normally trims the space in
    # "KEY= value", but a stray space inside an auth header is a silent 401 that
    # looks exactly like a wrong key, so this does not depend on it.
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_user_ids: frozenset[int] = field(
        default_factory=lambda: frozenset(
            int(x) for x in os.getenv("ALLOWED_USER_IDS", "").replace(" ", "").split(",") if x
        )
    )

    helius_key: str = os.getenv("HELIUS_API_KEY", "").strip()
    solscan_key: str = os.getenv("SOLSCAN_API_KEY", "").strip()
    etherscan_key: str = os.getenv("ETHERSCAN_API_KEY", "").strip()
    # Optional. Blockscout serves Base and other chains Etherscan's free plan
    # dropped, with no key at all; a free key from dev.blockscout.com only
    # raises its rate limits.
    blockscout_key: str = os.getenv("BLOCKSCOUT_API_KEY", "").strip()

    # How many 1000-signature pages the RPC fallback may walk back through when
    # hunting a token's first buyers. 0 means pick it from the RPC in use.
    rpc_max_pages: int = _int("RPC_MAX_PAGES", 0)
    # Solscan's free plan is 60 requests per minute. Raise it if you upgrade.
    solscan_rpm: int = _int("SOLSCAN_RPM", 60)

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

    # Seconds before a Telegram call gives up. The default of 5 is far too
    # tight on a slow link while a long scan holds the connection open.
    telegram_timeout: float = _float("TELEGRAM_TIMEOUT", 30)

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

    # Top traders on a coin: who turned a small entry into a large exit.
    traders_count: int = _int("TRADERS_COUNT", 5)
    # Track the coin's best traders as part of /harvest, not only its
    # earliest buyers. Being early is timing; a large multiple from a
    # small entry is timing plus the discipline to take it.
    harvest_traders: bool = _bool("HARVEST_TRADERS", True)
    # A floor on the entry, so a wallet that spent a fraction of a cent cannot
    # show a five-figure multiple and crowd out every real trade.
    trader_min_spend_usd: float = _float("TRADER_MIN_SPEND_USD", 1.0)
    # Pages of 100 transactions read from each end of a Solana coin's history.
    trader_scan_pages: int = _int("TRADER_SCAN_PAGES", 10)
    # EVM only: how many early buyers to price, and how many of each wallet's
    # trades to value. Each trade costs one call, so this bounds the wait.
    trader_cohort: int = _int("TRADER_COHORT", 15)
    # Pages of each Blockscout listing read per wallet.
    trader_pages: int = _int("TRADER_PAGES", 2)

    db_path: str = os.getenv("DB_PATH", "memehunter.db")

    # Name resolution. Some networks answer api.telegram.org with an address
    # that is not Telegram's, and every call then fails with "All connection
    # attempts failed" -- which looks like the bot being offline rather than
    # one wrong lookup. When this is on, the bot resolves the names below over
    # HTTPS instead of trusting the network's DNS server. Plain DNS is not
    # enough: forged replies race the real ones on port 53 whoever you ask.
    dns_fix: bool = _bool("DNS_FIX", True)
    dns_resolvers: tuple[str, ...] = field(
        default_factory=lambda: _csv("DNS_RESOLVERS", DOH_RESOLVERS))
    dns_hosts: tuple[str, ...] = field(
        default_factory=lambda: _csv("DNS_OVERRIDE_HOSTS", ("api.telegram.org",)))

    # Automatic backups are OFF by default: on a machine with a real disk they
    # just drip database files into your chat every few hours for no benefit.
    # Turn them on (BACKUP_ENABLED=1) when deploying somewhere with no
    # persistent disk, which is the only place they earn their keep.
    # /backup and /restore work by hand either way.
    backup_enabled: bool = _bool("BACKUP_ENABLED", False)
    backup_interval_hours: float = _float("BACKUP_INTERVAL_HOURS", 6)
    backup_chat_id: int = _int("BACKUP_CHAT_ID", 0)
    # Restore automatically when the database comes up empty. Off means a wiped
    # host stays wiped until you run /restore yourself.
    backup_auto_restore: bool = _bool("BACKUP_AUTO_RESTORE", True)

    @property
    def solana_rpc(self) -> str:
        explicit = os.getenv("SOLANA_RPC_URL", "").strip()
        if explicit:
            return explicit
        if self.helius_key:
            return f"https://mainnet.helius-rpc.com/?api-key={self.helius_key}"
        return "https://api.mainnet-beta.solana.com"

    @property
    def early_buyer_max_pages(self) -> int:
        """How deep the signature walk may go before giving up.

        The public RPC is slow and rate limited, so 30 pages (30k transactions)
        is about all it can manage. A private RPC is neither, and each page is
        one 1,000-signature call -- roughly one Helius credit out of a million
        free per month. Walking 200 pages costs 200 credits and covers most
        coins outright, which makes a Solscan key a nice-to-have rather than a
        requirement. Override with RPC_MAX_PAGES.
        """
        if self.rpc_max_pages > 0:
            return self.rpc_max_pages
        private_rpc = bool(self.helius_key or os.getenv("SOLANA_RPC_URL", "").strip())
        return 200 if private_rpc else 30

    def missing_required(self) -> list[str]:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_user_ids:
            missing.append("ALLOWED_USER_IDS")
        return missing


CFG = Config()
