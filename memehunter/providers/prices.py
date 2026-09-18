"""DexScreener: free, keyless market data. Used for pricing, liquidity and scoring."""
from __future__ import annotations

from .base import HttpClient, TokenMarket

DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"
DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/top/v1"
# DexScreener allows ~300 req/min; 30 token addresses per call.
BATCH = 30


class DexScreener:
    def __init__(self) -> None:
        self.http = HttpClient(rate=4, per=1.0)

    async def markets(self, addresses: list[str]) -> dict[str, TokenMarket]:
        """Best (deepest-liquidity) pair per token address."""
        out: dict[str, TokenMarket] = {}
        uniq = list(dict.fromkeys(a for a in addresses if a))
        for i in range(0, len(uniq), BATCH):
            chunk = uniq[i:i + BATCH]
            try:
                data = await self.http.get(DEX_TOKENS + ",".join(chunk))
            except Exception:
                continue
            for pair in (data or {}).get("pairs") or []:
                base = pair.get("baseToken") or {}
                addr = base.get("address") or ""
                if not addr:
                    continue
                liq = float((pair.get("liquidity") or {}).get("usd") or 0)
                prev = out.get(addr)
                if prev and prev.liquidity_usd >= liq:
                    continue
                txns = (pair.get("txns") or {}).get("h24") or {}
                change = pair.get("priceChange") or {}
                out[addr] = TokenMarket(
                    address=addr,
                    chain=pair.get("chainId", ""),
                    symbol=base.get("symbol", ""),
                    name=base.get("name", ""),
                    price_usd=float(pair.get("priceUsd") or 0),
                    liquidity_usd=liq,
                    fdv=float(pair.get("fdv") or pair.get("marketCap") or 0),
                    volume_24h=float((pair.get("volume") or {}).get("h24") or 0),
                    price_change_24h=float(change.get("h24") or 0),
                    price_change_6h=float(change.get("h6") or 0),
                    buys_24h=int(txns.get("buys") or 0),
                    sells_24h=int(txns.get("sells") or 0),
                    pair_created_ms=int(pair.get("pairCreatedAt") or 0),
                    pair_url=pair.get("url", ""),
                    raw=pair,
                )
        # Case-insensitive lookup for EVM addresses.
        for addr in list(out):
            out.setdefault(addr.lower(), out[addr])
        return out

    async def market(self, address: str) -> TokenMarket | None:
        res = await self.markets([address])
        return res.get(address) or res.get(address.lower())

    async def boosted(self, chain: str = "solana", limit: int = 30) -> list[TokenMarket]:
        """Tokens currently paying for promotion on DexScreener.

        Read this for what it is. It is an attention list, not a list of past
        winners -- somebody paid to put each of these in front of you, which is
        the opposite of the method's step 1. It is here because scanning for
        "coins that already did 100x" is not something any free API exposes, and
        skimming a live list beats typing addresses from memory. You still pick.
        """
        try:
            data = await self.http.get(DEX_BOOSTS)
        except Exception:
            return []
        addresses = [
            entry.get("tokenAddress") for entry in (data or [])
            if isinstance(entry, dict)
            and entry.get("chainId") == chain
            and entry.get("tokenAddress")
        ]
        if not addresses:
            return []
        markets = await self.markets(addresses[:limit])
        seen: set[str] = set()
        out: list[TokenMarket] = []
        for addr in addresses[:limit]:
            m = markets.get(addr) or markets.get(addr.lower())
            if m and m.address not in seen:
                seen.add(m.address)
                out.append(m)
        out.sort(key=lambda m: m.volume_24h, reverse=True)
        return out

    async def aclose(self) -> None:
        await self.http.aclose()
