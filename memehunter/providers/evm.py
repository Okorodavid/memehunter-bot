"""EVM data access via the Etherscan V2 multichain API (one key covers every chain)."""
from __future__ import annotations

import statistics
import time
from typing import Any

from ..config import CFG
from .base import EVM_CHAINS, IGNORE_LOWER, EarlyBuyer, Holding, HttpClient, WalletProfile
from .solana import classify_bot

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"


class EvmProvider:
    def __init__(self, chain: str = "ethereum") -> None:
        self.chain = chain if chain in EVM_CHAINS else "ethereum"
        self.chainid = EVM_CHAINS[self.chain]["chainid"]
        self.explorer = EVM_CHAINS[self.chain]["explorer"]
        self.decimals = EVM_CHAINS[self.chain].get("decimals", 18)
        self.http = HttpClient(rate=4, per=1.0)

    @property
    def enabled(self) -> bool:
        return bool(CFG.etherscan_key)

    async def _call(self, module: str, action: str, **params: Any) -> Any:
        query = {
            "chainid": self.chainid, "module": module, "action": action,
            "apikey": CFG.etherscan_key, **params,
        }
        data = await self.http.get(ETHERSCAN_V2, params=query)
        if not isinstance(data, dict):
            return []
        if str(data.get("status")) != "1":
            # "No transactions found" is a normal empty result, not an error.
            return []
        return data.get("result") or []

    # ------------------------------------------------------- step 2: early buyers
    async def early_buyers(self, token: str, limit: int) -> list[EarlyBuyer]:
        rows = await self._call(
            "account", "tokentx", contractaddress=token,
            page=1, offset=400, sort="asc", startblock=0, endblock=99999999,
        )
        if not rows:
            return []

        # Whoever repeatedly *sends* the token this early is the LP pair or the
        # deployer distributing supply -- never a buyer.
        senders: dict[str, int] = {}
        for r in rows:
            senders[(r.get("from") or "").lower()] = senders.get((r.get("from") or "").lower(), 0) + 1
        distributors = {a for a, c in senders.items() if c >= 5}

        buyers: dict[str, int] = {}
        for r in rows:
            to = (r.get("to") or "").lower()
            ts = int(r.get("timeStamp") or 0)
            if not to or to in IGNORE_LOWER or to in distributors or to == token.lower():
                continue
            buyers.setdefault(to, ts)
            if len(buyers) >= limit:
                break

        ordered = sorted(buyers.items(), key=lambda kv: kv[1])[:limit]
        return [
            EarlyBuyer(address=a, rank=i + 1, first_ts=ts, chain=self.chain)
            for i, (a, ts) in enumerate(ordered)
        ]

    # ------------------------------------------- steps 3 & 4: activity + bot check
    async def wallet_profile(self, address: str) -> WalletProfile:
        prof = WalletProfile(address=address, chain=self.chain)
        rows = await self._call(
            "account", "txlist", address=address,
            page=1, offset=300, sort="desc", startblock=0, endblock=99999999,
        )
        tokens = await self._call(
            "account", "tokentx", address=address,
            page=1, offset=300, sort="desc", startblock=0, endblock=99999999,
        )
        times = sorted(
            {int(r.get("timeStamp") or 0) for r in list(rows) + list(tokens)} - {0},
            reverse=True,
        )
        if not times:
            prof.dead = True
            return prof

        prof.last_active_ts = times[0]
        prof.dead = prof.days_since_active > CFG.active_window_days
        window = time.time() - CFG.active_window_days * 86400
        recent = [t for t in times if t >= window]
        prof.tx_count_30d = len(recent)
        gaps = [a - b for a, b in zip(recent, recent[1:])] if len(recent) > 1 else []
        if gaps:
            prof.median_gap_sec = float(statistics.median(gaps))
            prof.min_gap_sec = float(min(gaps))
        classify_bot(prof)
        return prof

    # ------------------------------------------------ step 5: recent acquisitions
    async def wallet_recent_buys(self, address: str, days: int) -> list[Holding]:
        cutoff = time.time() - days * 86400
        rows = await self._call(
            "account", "tokentx", address=address,
            page=1, offset=500, sort="desc", startblock=0, endblock=99999999,
        )
        out: dict[str, Holding] = {}
        for r in rows:
            ts = int(r.get("timeStamp") or 0)
            if ts < cutoff:
                break
            if (r.get("to") or "").lower() != address.lower():
                continue
            token = (r.get("contractAddress") or "").lower()
            if not token or token in IGNORE_LOWER:
                continue
            try:
                amount = int(r.get("value") or 0) / (10 ** int(r.get("tokenDecimal") or 18))
            except (ValueError, ZeroDivisionError):
                amount = 0.0
            h = out.setdefault(
                token,
                Holding(token=token, symbol=r.get("tokenSymbol", ""), first_seen_ts=ts),
            )
            h.amount += amount
            h.first_seen_ts = min(h.first_seen_ts or ts, ts)
        return list(out.values())

    async def wallet_holdings(self, address: str) -> list[Holding]:
        """Etherscan has no free balance-sheet endpoint, so net the transfer log."""
        rows = await self._call(
            "account", "tokentx", address=address,
            page=1, offset=1000, sort="desc", startblock=0, endblock=99999999,
        )
        net: dict[str, Holding] = {}
        for r in rows:
            token = (r.get("contractAddress") or "").lower()
            if not token or token in IGNORE_LOWER:
                continue
            try:
                amount = int(r.get("value") or 0) / (10 ** int(r.get("tokenDecimal") or 18))
            except (ValueError, ZeroDivisionError):
                continue
            h = net.setdefault(
                token, Holding(token=token, symbol=r.get("tokenSymbol", ""),
                               first_seen_ts=int(r.get("timeStamp") or 0)))
            if (r.get("to") or "").lower() == address.lower():
                h.amount += amount
            else:
                h.amount -= amount
        return [h for h in net.values() if h.amount > 0]

    async def native_balance(self, address: str) -> float:
        res = await self._call("account", "balance", address=address, tag="latest")
        try:
            # Not every chain's gas token has 18 decimals -- Arc charges gas in
            # USDC, which has 6. Hardcoding 1e18 would overstate a balance by a
            # factor of a trillion.
            return int(res) / (10 ** self.decimals)
        except (TypeError, ValueError):
            return 0.0

    async def token_safety(self, token: str) -> dict[str, Any]:
        """Only the cheap signal Etherscan gives for free: is the source verified."""
        res = await self._call("contract", "getsourcecode", address=token)
        entry = (res or [{}])[0] if isinstance(res, list) else {}
        return {
            "verified": bool(entry.get("SourceCode")),
            "contract_name": entry.get("ContractName") or "",
            "proxy": str(entry.get("Proxy") or "0") == "1",
        }

    async def aclose(self) -> None:
        await self.http.aclose()
