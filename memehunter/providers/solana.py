"""Solana data access: earliest buyers, wallet profiling, holdings, token safety.

Three tiers of sourcing, best-effort in this order:
  1. Solscan Pro  -- ascending transfer history, cheapest way to get *earliest* buyers
  2. Helius       -- enhanced/parsed transactions for recent swaps by a wallet
  3. Plain RPC    -- always available fallback, just slower (signature pagination)
"""
from __future__ import annotations

import asyncio
import logging
import statistics
import time
from typing import Any

from ..config import CFG
from .base import SOL_QUOTE_MINTS, EarlyBuyer, Holding, HttpClient, WalletProfile

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022 = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
SOLSCAN = "https://pro-api.solscan.io/v2.0"
HELIUS_API = "https://api.helius.xyz/v0"
# Solana added transaction version 1 in 2026. Asking for at most version 0 makes
# the RPC answer every newer transaction with an error instead of the
# transaction, which read as "no buyers" on any coin launched since.
MAX_TX_VERSION = 1
SYSTEM_PROGRAM = "11111111111111111111111111111111"

# Programs / system accounts that appear as transfer counterparties but never trade.
NON_WALLETS = {
    TOKEN_PROGRAM, TOKEN_2022, "11111111111111111111111111111111",
    "ComputeBudget111111111111111111111111111111",
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
}


log = logging.getLogger("memehunter.solana")


class HistoryTooDeep(RuntimeError):
    """Raised when the RPC fallback cannot reach a token's first transactions."""


class SolanaProvider:
    _solscan_note = ""
    _solscan_headers: dict[str, str] | None = None

    def __init__(self) -> None:
        self.rpc_url = CFG.solana_rpc
        self.http = HttpClient(rate=8, per=1.0)
        # Solscan's free plan allows 60 requests per MINUTE, not per second.
        # Sharing the RPC client's 8/s would 429 immediately, which reads like
        # a broken key, so Solscan gets its own much slower limiter.
        self.solscan_http = HttpClient(rate=max(CFG.solscan_rpm, 1), per=60.0)
        self._id = 0

    # ------------------------------------------------------------------ raw RPC
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    async def rpc(self, method: str, params: list[Any]) -> Any:
        body = {"jsonrpc": "2.0", "id": self._next_id(), "method": method, "params": params}
        data = await self.http.post(self.rpc_url, json=body)
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"RPC {method}: {data['error'].get('message')}")
        return (data or {}).get("result")

    async def rpc_batch(self, calls: list[tuple[str, list[Any]]]) -> list[Any]:
        """One HTTP round trip for many RPC calls; results come back in request order."""
        if not calls:
            return []
        out: list[Any] = []
        for i in range(0, len(calls), 20):
            chunk = calls[i:i + 20]
            body = [
                {"jsonrpc": "2.0", "id": self._next_id(), "method": m, "params": p}
                for m, p in chunk
            ]
            try:
                data = await self.http.post(self.rpc_url, json=body)
            except Exception:
                out.extend([None] * len(chunk))
                continue
            by_id = {d.get("id"): d for d in (data or []) if isinstance(d, dict)}
            for req in body:
                res = by_id.get(req["id"]) or {}
                out.append(res.get("result") if not res.get("error") else None)
        return out

    # ------------------------------------------------------- step 2: early buyers
    async def early_buyers(self, mint: str, limit: int) -> list[EarlyBuyer]:
        # Why Solscan did not answer, carried into the fallback's error message.
        # Swallowing this silently meant a failing key produced "set SOLSCAN_API_KEY"
        # -- telling you to do the thing you had already done.
        if not CFG.solscan_key:
            self._solscan_note = ("SOLSCAN_API_KEY is not set in .env, or the bot was "
                                  "started before you added it")
        else:
            try:
                buyers = await self._early_buyers_solscan(mint, limit)
                if buyers:
                    return buyers
                self._solscan_note = ("your Solscan key works but returned no transfers "
                                      "for this token")
                log.warning("solscan returned no transfers for %s", mint)
            except Exception as exc:
                if self._is_auth_error(exc):
                    said = self._api_message(exc)
                    self._solscan_note = (
                        "Solscan refused your key (401/403"
                        + (f", it said: {said}" if said else "")
                        + "). The key reached them and was rejected, so it is the "
                        "key or the account - the Pro API needs a paid plan. Check "
                        "solscan.io/user/profile#api_management"
                    )
                else:
                    self._solscan_note = f"the Solscan request failed ({exc})"
                log.warning("solscan early_buyers failed for %s: %r", mint, exc)

        # Helius can read a token's history oldest-first in one call, which is
        # exactly the question "who bought first" -- no walking back through
        # every transaction since. Any coin, any size, a handful of credits.
        if self.helius_rpc:
            try:
                buyers = await self._early_buyers_helius(mint, limit)
                if buyers:
                    return buyers
                log.warning("helius returned no early buyers for %s", mint)
            except Exception as exc:
                log.warning("helius early_buyers failed for %s: %r", mint, exc)

        return await self._early_buyers_rpc(mint, limit)

    @property
    def helius_rpc(self) -> bool:
        return "helius" in self.rpc_url.lower()

    async def _early_buyers_helius(self, mint: str, limit: int) -> list[EarlyBuyer]:
        """Helius `getTransactionsForAddress` with sortOrder=asc: the first
        transactions ever to touch the mint, already parsed."""
        buyers: dict[str, int] = {}
        page_token = None
        for _ in range(4):                       # up to 400 transactions
            opts: dict[str, Any] = {
                "sortOrder": "asc", "limit": 100, "transactionDetails": "full",
                "encoding": "jsonParsed",
                "maxSupportedTransactionVersion": MAX_TX_VERSION,
            }
            if page_token:
                opts["paginationToken"] = page_token
            res = await self.rpc("getTransactionsForAddress", [mint, opts])
            if not isinstance(res, dict):
                break
            data = res.get("data") or []
            for tx in data:
                if (tx.get("meta") or {}).get("err"):
                    continue
                ts = int(tx.get("blockTime") or 0)
                for owner in self._owners_gaining(tx, mint):
                    buyers.setdefault(owner, ts)
            page_token = res.get("paginationToken")
            if len(buyers) >= limit + 5 or not page_token or len(data) < 100:
                break
        return await self._rank_buyers(buyers, limit)

    async def _rank_buyers(self, buyers: dict[str, int], limit: int) -> list[EarlyBuyer]:
        """Earliest first, minus the accounts that are programs rather than people."""
        wallets = await self._only_wallets(list(buyers))
        ordered = sorted(((a, t) for a, t in buyers.items() if a in wallets),
                         key=lambda kv: kv[1])[:limit]
        return [
            EarlyBuyer(address=a, rank=i + 1, first_ts=ts, chain="solana")
            for i, (a, ts) in enumerate(ordered)
        ]

    async def _only_wallets(self, addresses: list[str]) -> set[str]:
        """Drop bonding curves, pool vault authorities and other program-owned
        accounts. They receive tokens in the first transactions of every launch
        and would otherwise sit at the top of the "early buyers" list.

        A person's wallet is owned by the System Program. If the lookup itself
        fails, keep everyone: a noisy list beats an empty one.
        """
        keep: set[str] = set()
        for i in range(0, len(addresses), 100):
            chunk = addresses[i:i + 100]
            try:
                res = await self.rpc("getMultipleAccounts",
                                     [chunk, {"encoding": "base64"}]) or {}
                values = res.get("value") or []
            except Exception:
                keep.update(chunk)
                continue
            if len(values) != len(chunk):
                keep.update(chunk)
                continue
            for addr, info in zip(chunk, values):
                if info is None or (info.get("owner") == SYSTEM_PROGRAM
                                    and not info.get("executable")):
                    keep.add(addr)
        return keep

    @staticmethod
    def _is_auth_error(exc: Exception) -> bool:
        resp = getattr(exc, "response", None)
        return getattr(resp, "status_code", None) in (401, 403)

    @staticmethod
    def _api_message(exc: Exception) -> str:
        """The message inside an API's error body, if it sent one."""
        resp = getattr(exc, "response", None)
        try:
            body = resp.json()
        except Exception:
            return ""
        if not isinstance(body, dict):
            return ""
        errors = body.get("errors")
        msg = errors.get("message") if isinstance(errors, dict) else None
        return str(msg or body.get("message") or "")[:160]

    async def _solscan_get(self, path: str, params: dict[str, Any]) -> Any:
        """GET from Solscan using whichever auth header this key is accepted with.

        Solscan has used more than one header name across API versions, and a
        mismatch is indistinguishable from a dead key -- both are a bare 401. So
        rather than hardcode a guess, try each once and remember what worked.
        If every variant is refused, the key itself is the problem (most often
        an account with no active plan) and the caller says so.
        """
        key = CFG.solscan_key
        variants = ([self._solscan_headers] if self._solscan_headers else
                    [{"token": key}, {"Authorization": f"Bearer {key}"}, {"X-Token": key}])
        last: Exception | None = None
        for headers in variants:
            try:
                # retries=1: a 401 is a verdict, not a blip, so do not back off
                # three times before trying the next header.
                data = await self.solscan_http.get(f"{SOLSCAN}{path}", headers=headers,
                                                   params=params, retries=1)
                if self._solscan_headers != headers:
                    log.info("solscan accepted the %s header", next(iter(headers)))
                    self._solscan_headers = headers
                return data
            except Exception as exc:
                last = exc
                if not self._is_auth_error(exc):
                    raise
        self._solscan_headers = None
        raise last or RuntimeError("solscan request failed")

    async def _early_buyers_solscan(self, mint: str, limit: int) -> list[EarlyBuyer]:
        seen: dict[str, int] = {}
        for page in range(1, 6):
            data = await self._solscan_get(
                "/token/transfer",
                {
                    "address": mint, "page": page, "page_size": 100,
                    "sort_by": "block_time", "sort_order": "asc",
                },
            )
            rows = (data or {}).get("data") or []
            if not rows:
                break
            for row in rows:
                to = row.get("to_address") or ""
                ts = int(row.get("block_time") or 0)
                if not to or to in NON_WALLETS or to in SOL_QUOTE_MINTS or to == mint:
                    continue
                seen.setdefault(to, ts)
            if len(seen) >= limit:
                break
        ordered = sorted(seen.items(), key=lambda kv: kv[1])[:limit]
        return [
            EarlyBuyer(address=a, rank=i + 1, first_ts=ts, chain="solana")
            for i, (a, ts) in enumerate(ordered)
        ]

    async def _early_buyers_rpc(self, mint: str, limit: int,
                                max_pages: int | None = None) -> list[EarlyBuyer]:
        """Walk signatures backwards to the very first ones, then parse those txs."""
        if max_pages is None:
            max_pages = CFG.early_buyer_max_pages
        before: str | None = None
        oldest: list[dict] = []
        reached_genesis = False
        for _ in range(max_pages):
            params: dict[str, Any] = {"limit": 1000}
            if before:
                params["before"] = before
            sigs = await self.rpc("getSignaturesForAddress", [mint, params]) or []
            if not sigs:
                reached_genesis = True
                break
            oldest = sigs  # last non-empty page is the earliest slice we reached
            before = sigs[-1]["signature"]
            if len(sigs) < 1000:
                reached_genesis = True
                break
            await asyncio.sleep(0)

        # Without reaching the first signature these are just "old" buyers, not
        # *early* ones -- returning them would quietly answer the wrong question.
        if not reached_genesis:
            note = getattr(self, "_solscan_note", "")
            raise HistoryTooDeep(
                f"This token has more than {max_pages * 1000:,} transactions, so the "
                f"public RPC cannot walk back to its first buyers."
                + (f"\n\nSolscan was not usable here: {note}." if note else "")
                + f"\n\nA Helius RPC (HELIUS_API_KEY in .env) reads any coin's first "
                  f"buyers directly, whatever its size. Otherwise raise RPC_MAX_PAGES "
                  f"(currently {max_pages}) or try a smaller coin."
            )

        # Chronological order, then parse enough transactions to fill `limit` buyers.
        chrono = [s for s in reversed(oldest) if not s.get("err")]
        buyers: dict[str, int] = {}
        for i in range(0, min(len(chrono), 160), 20):
            batch = chrono[i:i + 20]
            results = await self.rpc_batch([
                ("getTransaction",
                 [s["signature"], {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": MAX_TX_VERSION}])
                for s in batch
            ])
            for sig_info, tx in zip(batch, results):
                ts = int(sig_info.get("blockTime") or 0)
                for owner in self._owners_gaining(tx, mint):
                    buyers.setdefault(owner, ts)
            if len(buyers) >= limit + 5:
                break

        return await self._rank_buyers(buyers, limit)

    @staticmethod
    def _owners_gaining(tx: dict | None, mint: str) -> list[str]:
        """Owners whose balance of `mint` increased in this transaction."""
        if not tx:
            return []
        meta = tx.get("meta") or {}
        pre = {
            (b.get("owner"), b.get("mint")): float(
                (b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            for b in meta.get("preTokenBalances") or []
        }
        gained = []
        for b in meta.get("postTokenBalances") or []:
            if b.get("mint") != mint:
                continue
            owner = b.get("owner")
            if not owner or owner in NON_WALLETS:
                continue
            after = float((b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
            if after > pre.get((owner, mint), 0.0):
                gained.append(owner)
        return gained

    # ------------------------------------------- steps 3 & 4: activity + bot check
    async def wallet_profile(self, address: str) -> WalletProfile:
        prof = WalletProfile(address=address, chain="solana")
        try:
            sigs = await self.rpc(
                "getSignaturesForAddress", [address, {"limit": 300}]) or []
        except Exception:
            prof.dead = True
            return prof
        times = sorted((int(s["blockTime"]) for s in sigs if s.get("blockTime")), reverse=True)
        if not times:
            prof.dead = True
            return prof

        now = time.time()
        prof.last_active_ts = times[0]
        prof.dead = prof.days_since_active > CFG.active_window_days
        window = now - CFG.active_window_days * 86400
        recent = [t for t in times if t >= window]
        prof.tx_count_30d = len(recent)

        gaps = [a - b for a, b in zip(recent, recent[1:])] if len(recent) > 1 else []
        if gaps:
            prof.median_gap_sec = float(statistics.median(gaps))
            prof.min_gap_sec = float(min(gaps))
        classify_bot(prof)
        return prof

    # --------------------------------------------- step 5: what are they holding
    async def wallet_holdings(self, address: str) -> list[Holding]:
        calls = [
            ("getTokenAccountsByOwner",
             [address, {"programId": prog}, {"encoding": "jsonParsed"}])
            for prog in (TOKEN_PROGRAM, TOKEN_2022)
        ]
        results = await self.rpc_batch(calls)
        out: dict[str, Holding] = {}
        for res in results:
            for acc in (res or {}).get("value") or []:
                info = (((acc.get("account") or {}).get("data") or {})
                        .get("parsed") or {}).get("info") or {}
                mint = info.get("mint")
                amt = float((info.get("tokenAmount") or {}).get("uiAmount") or 0)
                if not mint or amt <= 0 or mint in SOL_QUOTE_MINTS:
                    continue
                cur = out.get(mint)
                out[mint] = Holding(token=mint, amount=amt + (cur.amount if cur else 0))
        return list(out.values())

    async def native_balance(self, address: str) -> float:
        result = await self.rpc("getBalance", [address])
        return float(((result or {}).get("value") or 0)) / 1e9

    async def wallet_recent_buys(self, address: str, days: int) -> list[Holding]:
        """Tokens the wallet *acquired* inside the window, not merely still holds."""
        cutoff = time.time() - days * 86400
        if CFG.helius_key:
            try:
                return await self._recent_buys_helius(address, cutoff)
            except Exception:
                pass
        return await self._recent_buys_rpc(address, cutoff)

    async def _recent_buys_helius(self, address: str, cutoff: float) -> list[Holding]:
        out: dict[str, Holding] = {}
        before = None
        for _ in range(3):
            params: dict[str, Any] = {"api-key": CFG.helius_key, "limit": 100, "type": "SWAP"}
            if before:
                params["before"] = before
            txs = await self.http.get(
                f"{HELIUS_API}/addresses/{address}/transactions", params=params) or []
            if not txs:
                break
            for tx in txs:
                ts = int(tx.get("timestamp") or 0)
                if ts < cutoff:
                    return list(out.values())
                for tr in tx.get("tokenTransfers") or []:
                    mint = tr.get("mint")
                    if (tr.get("toUserAccount") == address and mint
                            and mint not in SOL_QUOTE_MINTS):
                        h = out.setdefault(mint, Holding(token=mint, first_seen_ts=ts))
                        h.amount += float(tr.get("tokenAmount") or 0)
                        h.first_seen_ts = min(h.first_seen_ts or ts, ts)
            before = txs[-1].get("signature")
        return list(out.values())

    async def _recent_buys_rpc(self, address: str, cutoff: float) -> list[Holding]:
        sigs = await self.rpc("getSignaturesForAddress", [address, {"limit": 150}]) or []
        fresh = [s for s in sigs
                 if not s.get("err") and int(s.get("blockTime") or 0) >= cutoff]
        out: dict[str, Holding] = {}
        for i in range(0, len(fresh), 20):
            batch = fresh[i:i + 20]
            results = await self.rpc_batch([
                ("getTransaction",
                 [s["signature"], {"encoding": "jsonParsed",
                                   "maxSupportedTransactionVersion": MAX_TX_VERSION}])
                for s in batch
            ])
            for sig_info, tx in zip(batch, results):
                ts = int(sig_info.get("blockTime") or 0)
                for mint, delta in self._mint_deltas_for_owner(tx, address):
                    if delta <= 0 or mint in SOL_QUOTE_MINTS:
                        continue
                    h = out.setdefault(mint, Holding(token=mint, first_seen_ts=ts))
                    h.amount += delta
                    h.first_seen_ts = min(h.first_seen_ts or ts, ts)
        return list(out.values())

    @staticmethod
    def _mint_deltas_for_owner(tx: dict | None, owner: str) -> list[tuple[str, float]]:
        if not tx:
            return []
        meta = tx.get("meta") or {}
        pre: dict[str, float] = {}
        for b in meta.get("preTokenBalances") or []:
            if b.get("owner") == owner:
                pre[b.get("mint")] = pre.get(b.get("mint"), 0.0) + float(
                    (b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        post: dict[str, float] = {}
        for b in meta.get("postTokenBalances") or []:
            if b.get("owner") == owner:
                post[b.get("mint")] = post.get(b.get("mint"), 0.0) + float(
                    (b.get("uiTokenAmount") or {}).get("uiAmount") or 0)
        return [(m, v - pre.get(m, 0.0)) for m, v in post.items() if v - pre.get(m, 0.0) > 0]

    # -------------------------------------------------- step 7 input: rug checks
    async def token_safety(self, mint: str) -> dict[str, Any]:
        info, supply, largest = await self.rpc_batch([
            ("getAccountInfo", [mint, {"encoding": "jsonParsed"}]),
            ("getTokenSupply", [mint]),
            ("getTokenLargestAccounts", [mint]),
        ])
        parsed = ((((info or {}).get("value") or {}).get("data") or {})
                  .get("parsed") or {}).get("info") or {}
        total = float((((supply or {}).get("value")) or {}).get("uiAmount") or 0)
        accounts = ((largest or {}).get("value")) or []
        amounts = sorted(
            (float((a.get("uiAmount") or 0)) for a in accounts), reverse=True)
        # The deepest account is almost always the LP vault, so report both readings.
        top10 = sum(amounts[:10]) / total if total else 0.0
        top10_ex_lp = sum(amounts[1:11]) / total if total else 0.0
        return {
            "mint_authority": parsed.get("mintAuthority"),
            "freeze_authority": parsed.get("freezeAuthority"),
            "decimals": parsed.get("decimals"),
            "supply": total,
            "top10_share": top10,
            "top10_share_ex_largest": top10_ex_lp,
        }

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.solscan_http.aclose()


def classify_bot(prof: WalletProfile) -> None:
    """Step 4 heuristics: speed and volume are what separate a bot from a human."""
    reasons = []
    if prof.median_gap_sec and prof.median_gap_sec < CFG.bot_min_median_gap_sec:
        reasons.append(f"median gap {prof.median_gap_sec:.1f}s between txs")
    per_day = prof.tx_count_30d / max(CFG.active_window_days, 1)
    if per_day > CFG.bot_max_tx_per_day:
        reasons.append(f"{per_day:.0f} tx/day")
    if prof.min_gap_sec and prof.min_gap_sec < 1 and prof.tx_count_30d > 200:
        reasons.append("sub-second bursts at high volume")
    prof.is_bot = bool(reasons)
    prof.bot_reason = "; ".join(reasons)
