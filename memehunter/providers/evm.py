"""EVM data access.

Etherscan V2 first: one key, the chain picked by chainid. In 2026 its free plan
stopped covering several chains -- Base, BSC and Avalanche answer every call
with "Free API access is not supported for this chain" -- so a chain with a
keyless public explorer falls back to that automatically. Blockscout instances
speak Etherscan's own query dialect at /api and a richer REST API at /api/v2;
Routescan speaks the Etherscan dialect for Avalanche. The rest of the bot never
sees the difference: every path returns rows in Etherscan's shape.
"""
from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime
from typing import Any

import httpx

from ..config import CFG
from .base import EVM_CHAINS, IGNORE_LOWER, EarlyBuyer, Holding, HttpClient, WalletProfile
from .solana import classify_bot

ETHERSCAN_V2 = "https://api.etherscan.io/v2/api"

# Keyless explorers, per chain. "legacy" is the Etherscan-dialect endpoint,
# "v2" Blockscout's REST API. BSC, Robinhood Chain and Arc have no usable
# public instance (Robinhood's sits behind a browser challenge), so those
# need an Etherscan plan that covers them.
FALLBACK: dict[str, dict[str, str]] = {
    "ethereum": {"legacy": "https://eth.blockscout.com/api",
                 "v2": "https://eth.blockscout.com/api/v2"},
    "base": {"legacy": "https://base.blockscout.com/api",
             "v2": "https://base.blockscout.com/api/v2"},
    "arbitrum": {"legacy": "https://arbitrum.blockscout.com/api",
                 "v2": "https://arbitrum.blockscout.com/api/v2"},
    "polygon": {"legacy": "https://polygon.blockscout.com/api",
                "v2": "https://polygon.blockscout.com/api/v2"},
    "avalanche": {"legacy": "https://api.routescan.io/v2/network/mainnet/evm/43114/etherscan/api"},
}

# Public JSON-RPC nodes, used on a fallback chain for one job: reading a token's
# first Transfer events straight from the chain, oldest first. Explorer REST
# APIs only list transfers newest-first, and Blockscout's Etherscan-dialect
# endpoint that can sort ascending allows about ten keyless calls an hour.
PUBLIC_RPC = {
    # Ethereum is absent on purpose: the public nodes tried refuse or empty out
    # eth_getLogs, and Etherscan's free plan still covers Ethereum anyway.
    "base": "https://mainnet.base.org",
    "arbitrum": "https://arb1.arbitrum.io/rpc",
    "polygon": "https://polygon-rpc.com",
}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Etherscan's wording when the key is fine but the plan does not include the chain.
PLAN_BLOCKED = ("not supported for this chain", "upgrade your api plan")

# Blockscout's public /api allows only a handful of keyless calls a minute, so
# per-wallet work goes through /api/v2, which is far more generous. Each v2
# page is 50 rows; this caps how many pages one lookup may turn.
V2_PAGE = 50
V2_MAX_PAGES = 2

log = logging.getLogger("memehunter.evm")

# Etherscan says status "0" both for "there is nothing here" and for "that
# request failed". These messages are the harmless kind.
EMPTY_MESSAGES = {"no transactions found", "no records found", "no data found",
                  "no token transfers found", "no internal transactions found"}


def _iso_ts(value: Any) -> int:
    """Blockscout timestamps are ISO-8601 strings; Etherscan's are epoch seconds."""
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return 0


def _addr(value: Any) -> str:
    """Blockscout wraps addresses in objects; Etherscan gives the bare string."""
    if isinstance(value, dict):
        value = value.get("hash")
    return str(value or "").lower()


class EvmProvider:
    # Why the most recent call returned nothing, when it was not simply empty.
    last_error = ""

    def __init__(self, chain: str = "ethereum") -> None:
        self.chain = chain if chain in EVM_CHAINS else "ethereum"
        self.chainid = EVM_CHAINS[self.chain]["chainid"]
        self.explorer = EVM_CHAINS[self.chain]["explorer"]
        self.decimals = EVM_CHAINS[self.chain].get("decimals", 18)
        self.http = HttpClient(rate=4, per=1.0)
        self.v2_http = HttpClient(rate=4, per=1.0)
        self.fallback = FALLBACK.get(self.chain, {})
        # Flipped the first time Etherscan says the plan does not cover this chain.
        self._use_fallback = False

    @property
    def enabled(self) -> bool:
        return bool(CFG.etherscan_key or self.fallback)

    @property
    def using_fallback(self) -> bool:
        return bool(self.fallback) and (self._use_fallback or not CFG.etherscan_key)

    @property
    def source(self) -> str:
        """Which explorer is answering for this chain right now."""
        if not self.using_fallback:
            return "Etherscan"
        return "Routescan" if "routescan" in self.fallback.get("legacy", "") else "Blockscout"

    # ------------------------------------------------------------------ calls
    async def _call(self, module: str, action: str, **params: Any) -> Any:
        """Call the explorer, keeping the difference between "nothing here" and
        "that request failed".

        Etherscan answers both with status "0". Collapsing them into an empty
        list meant a rejected key, an exhausted rate limit and a chain your plan
        cannot reach all looked exactly like a token with no buyers -- which is
        the least useful thing a lookup can tell you.
        """
        if self.using_fallback:
            return await self._call_fallback(module, action, params)

        data = await self._call_legacy(ETHERSCAN_V2, module, action,
                                       {"apikey": CFG.etherscan_key, **params})
        if self.fallback and any(p in self.last_error.lower() for p in PLAN_BLOCKED):
            log.info("etherscan's plan does not cover %s; using %s instead",
                     self.chain, self.fallback["legacy"].split("/")[2])
            self._use_fallback = True
            return await self._call_fallback(module, action, params)
        return data

    async def _call_legacy(self, url: str, module: str, action: str,
                           params: dict[str, Any]) -> Any:
        """One Etherscan-dialect request: {status, message, result}."""
        query = {"chainid": self.chainid, "module": module, "action": action, **params}
        try:
            data = await self.http.get(url, params=query)
        except Exception as exc:
            host = url.split("/")[2]
            self.last_error = f"could not reach {host} ({exc})"
            log.warning("%s %s/%s on %s: %r", host, module, action, self.chain, exc)
            return []

        if not isinstance(data, dict):
            self.last_error = "the explorer returned something that was not JSON"
            return []

        if str(data.get("status")) == "1":
            self.last_error = ""
            return data.get("result") or []

        message = str(data.get("message") or "").strip()
        result = data.get("result")
        if message.lower() in EMPTY_MESSAGES:
            self.last_error = ""          # genuinely nothing to return
            return []

        # A real failure. Etherscan puts the useful text in `result` for these.
        detail = result if isinstance(result, str) and result else (message or "unknown")
        self.last_error = detail
        log.warning("%s %s/%s on %s (chainid %s): %s", url.split("/")[2],
                    module, action, self.chain, self.chainid, detail)
        return []

    async def _call_fallback(self, module: str, action: str, params: dict[str, Any]) -> Any:
        """Answer an Etherscan-shaped question from the keyless explorer."""
        v2 = self.fallback.get("v2")
        address = params.get("address")
        if v2 and module == "account" and address:
            pages = max(1, min(V2_MAX_PAGES, int(params.get("offset") or V2_PAGE) // V2_PAGE))
            if action == "txlist":
                return await self._v2_rows(f"{v2}/addresses/{address}/transactions",
                                           {}, pages, self._tx_row)
            if action == "tokentx":
                return await self._v2_rows(f"{v2}/addresses/{address}/token-transfers",
                                           {"type": "ERC-20"}, pages, self._transfer_row)
            if action == "balance":
                info = await self._v2_get(f"{v2}/addresses/{address}")
                return str((info or {}).get("coin_balance") or "0")
        if v2 and module == "contract" and action == "getsourcecode":
            info = await self._v2_get(f"{v2}/smart-contracts/{address}") or {}
            return [{"SourceCode": "verified" if info.get("is_verified") else "",
                     "ContractName": info.get("name") or "",
                     "Proxy": "1" if info.get("proxy_type") else "0"}]
        query = dict(params)
        if CFG.blockscout_key:
            query["apikey"] = CFG.blockscout_key
        return await self._call_legacy(self.fallback["legacy"], module, action, query)

    async def _v2_get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        query = dict(params or {})
        if CFG.blockscout_key:
            query["apikey"] = CFG.blockscout_key
        try:
            data = await self.v2_http.get(url, params=query)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self.last_error = ""      # an address or contract Blockscout has never seen
                return None
            self.last_error = f"{self.source} answered {exc.response.status_code}"
            log.warning("%s %s: %r", self.source, url, exc)
            return None
        except Exception as exc:
            self.last_error = f"could not reach {self.source} ({exc})"
            log.warning("%s %s: %r", self.source, url, exc)
            return None
        self.last_error = ""
        return data

    # ------------------------------------------------ first transfers via RPC
    async def _rpc(self, method: str, params: list[Any]) -> Any:
        url = PUBLIC_RPC[self.chain]
        data = await self.v2_http.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(data, dict):
            raise RuntimeError(f"{method}: no JSON-RPC response")
        if data.get("error"):
            raise RuntimeError(f"{method}: {data['error'].get('message')}")
        return data.get("result")

    async def _block_at(self, number: int | str) -> tuple[int, int]:
        tag = number if isinstance(number, str) else hex(number)
        block = await self._rpc("eth_getBlockByNumber", [tag, False]) or {}
        return int(block.get("number") or "0x0", 16), int(block.get("timestamp") or "0x0", 16)

    async def _creation_block(self, token: str, since_ts: int) -> tuple[int, int, float] | None:
        """(block, its timestamp, seconds per block) for where the token began.

        Blockscout knows the creating transaction for most contracts. A token
        minted inside a launchpad's factory call has none recorded, so then the
        DexScreener pair-creation time is walked to a block number instead.
        """
        latest, latest_ts = await self._block_at("latest")
        older, older_ts = await self._block_at(max(latest - 20000, 1))
        block_time = max((latest_ts - older_ts) / max(latest - older, 1), 0.1)

        v2 = self.fallback.get("v2")
        if v2:
            info = await self._v2_get(f"{v2}/addresses/{token}") or {}
            creation = info.get("creation_transaction_hash") or info.get("creation_tx_hash")
            tx = (await self._v2_get(f"{v2}/transactions/{creation}") or {}) if creation else {}
            try:
                born = int(tx.get("block_number"))
                return born, _iso_ts(tx.get("timestamp")) or since_ts, block_time
            except (TypeError, ValueError):
                pass

        if not since_ts:
            return None
        guess = latest - int((latest_ts - since_ts) / block_time)
        for _ in range(4):
            guess = max(1, min(guess, latest))
            number, ts = await self._block_at(guess)
            drift = since_ts - ts
            if abs(drift) < 2 * block_time:
                return number, ts, block_time
            guess = number + int(drift / block_time)
        return guess, since_ts, block_time

    async def _rpc_early_transfers(self, token: str, want: int, since_ts: int
                                   ) -> list[dict[str, Any]] | None:
        """The token's first Transfer events, oldest first, from the chain itself.

        eth_getLogs returns a range in block order, which is the order this
        whole method depends on. The window starts a little before the token's
        first block, because a pair can be created after the token, and widens
        when a quiet launch spread its first transfers over hours or days.
        Returns None when the chain has no public node here or nothing about the
        token's age is known, so the caller can try the explorer listing.
        """
        if self.chain not in PUBLIC_RPC:
            return None
        try:
            found = await self._creation_block(token, since_ts)
        except Exception as exc:
            log.warning("%s rpc: could not place %s in time: %r", self.chain, token, exc)
            return None
        if not found:
            return None
        born, born_ts, block_time = found

        lead = int(600 / block_time)             # ten minutes before the first block
        span = int(1200 / block_time)            # twenty minutes after it
        logs: list[dict[str, Any]] = []
        for _ in range(6):
            params = {"address": token, "topics": [TRANSFER_TOPIC],
                      "fromBlock": hex(max(born - lead, 1)), "toBlock": hex(born + span)}
            try:
                logs = await self._rpc("eth_getLogs", [params]) or []
            except Exception as exc:
                text = str(exc).lower()
                if any(w in text for w in ("range", "limit", "too many", "10000", "exceed")):
                    span = max(span // 4, 1)     # the node wants a smaller window
                    continue
                log.warning("%s rpc eth_getLogs for %s: %r", self.chain, token, exc)
                return None
            if len(logs) >= want:
                break
            lead *= 4
            span *= 4
        if not logs:
            return None

        rows = []
        for item in logs[:want]:
            topics = item.get("topics") or []
            if len(topics) < 3:
                continue
            block = int(item.get("blockNumber") or "0x0", 16)
            try:
                value = str(int(item.get("data") or "0x0", 16))
            except ValueError:
                value = "0"
            rows.append({
                "hash": item.get("transactionHash") or "",
                # Exact on chains with a fixed block time (Base, Arbitrum,
                # post-merge Ethereum); a few seconds out on Polygon. It only
                # orders buyers relative to each other, so that is enough.
                "timeStamp": int(born_ts + (block - born) * block_time),
                "from": "0x" + topics[1][-40:].lower(),
                "to": "0x" + topics[2][-40:].lower(),
                "contractAddress": token.lower(),
                "tokenSymbol": "", "tokenDecimal": "18", "value": value,
            })
        return rows

    async def _v2_rows(self, url: str, params: dict[str, Any], pages: int,
                       convert) -> list[dict[str, Any]]:
        """Newest first, like Etherscan's sort=desc, following next_page_params."""
        rows: list[dict[str, Any]] = []
        query = dict(params)
        for _ in range(pages):
            data = await self._v2_get(url, query)
            if not isinstance(data, dict):
                break
            rows.extend(convert(item) for item in data.get("items") or [])
            nxt = data.get("next_page_params")
            if not nxt:
                break
            query = {**params, **nxt}
        return rows

    @staticmethod
    def _tx_row(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "hash": item.get("hash") or "",
            "timeStamp": _iso_ts(item.get("timestamp")),
            "from": _addr(item.get("from")),
            "to": _addr(item.get("to")),
            "value": str(item.get("value") or "0"),
        }

    @staticmethod
    def _transfer_row(item: dict[str, Any]) -> dict[str, Any]:
        token = item.get("token") or {}
        return {
            "hash": item.get("transaction_hash") or "",
            "timeStamp": _iso_ts(item.get("timestamp")),
            "from": _addr(item.get("from")),
            "to": _addr(item.get("to")),
            "contractAddress": _addr(token.get("address_hash") or token.get("address")),
            "tokenSymbol": token.get("symbol") or "",
            "tokenDecimal": str(token.get("decimals") or "18"),
            "value": str((item.get("total") or {}).get("value") or "0"),
        }

    # ------------------------------------------------------- step 2: early buyers
    async def early_buyers(self, token: str, limit: int, since_ts: int = 0) -> list[EarlyBuyer]:
        """`since_ts` is when the token's pair appeared, if known. On a fallback
        chain it lets the first transfers be read from the chain itself."""
        rows: list[dict[str, Any]] = []
        if self.using_fallback:
            rows = await self._rpc_early_transfers(token, 400, since_ts) or []
        if not rows:
            rows = await self._call(
                "account", "tokentx", contractaddress=token,
                page=1, offset=400, sort="asc", startblock=0, endblock=99999999,
            )
            if not rows and self.using_fallback and self.chain in PUBLIC_RPC:
                # Etherscan handed the chain over during that call.
                rows = await self._rpc_early_transfers(token, 400, since_ts) or []
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
        """Current token balances.

        Blockscout keeps a balance sheet per address, so on a fallback chain
        this is one call. Etherscan has no free equivalent, so there the
        transfer log is netted instead.
        """
        if self.using_fallback and self.fallback.get("v2"):
            return await self._v2_holdings(address)
        rows = await self._call(
            "account", "tokentx", address=address,
            page=1, offset=1000, sort="desc", startblock=0, endblock=99999999,
        )
        if self.using_fallback and self.fallback.get("v2"):
            # Etherscan refused mid-call and the rows above are a short page
            # of transfers, not a full history. Ask the balance sheet instead.
            return await self._v2_holdings(address)
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

    async def _v2_holdings(self, address: str) -> list[Holding]:
        url = f"{self.fallback['v2']}/addresses/{address}/tokens"
        out: list[Holding] = []
        query: dict[str, Any] = {"type": "ERC-20"}
        for _ in range(3):
            data = await self._v2_get(url, query)
            if not isinstance(data, dict):
                break
            for item in data.get("items") or []:
                token = item.get("token") or {}
                addr = _addr(token.get("address_hash") or token.get("address"))
                if not addr or addr in IGNORE_LOWER:
                    continue
                try:
                    amount = int(item.get("value") or 0) / (10 ** int(token.get("decimals") or 18))
                except (ValueError, ZeroDivisionError):
                    continue
                if amount > 0:
                    out.append(Holding(token=addr, symbol=token.get("symbol") or "",
                                       amount=amount))
            nxt = data.get("next_page_params")
            if not nxt:
                break
            query = {"type": "ERC-20", **nxt}
        return out

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
        """Only the cheap signal an explorer gives for free: is the source verified."""
        res = await self._call("contract", "getsourcecode", address=token)
        entry = (res or [{}])[0] if isinstance(res, list) else {}
        return {
            "verified": bool(entry.get("SourceCode")),
            "contract_name": entry.get("ContractName") or "",
            "proxy": str(entry.get("Proxy") or "0") == "1",
        }

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.v2_http.aclose()
