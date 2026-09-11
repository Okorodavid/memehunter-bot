"""DexScreener: free, keyless market data. Used for pricing, liquidity and scoring."""
from __future__ import annotations

from .base import HttpClient, TokenMarket

DEX_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/"
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

    async def aclose(self) -> None:
        await self.http.aclose()
