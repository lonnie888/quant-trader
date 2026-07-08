"""Scan Binance USDT-margined perpetual top-N gainers over 24h.
Uses the public fapi endpoint (no API key needed)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import requests


@dataclass
class Gainer:
    symbol: str
    last: float
    pct_change_24h: float
    quote_volume_24h: float

    @property
    def market_cap_proxy(self) -> float:
        return self.quote_volume_24h


FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"


def scan_gainers(
    client=None,
    quote: str = "USDT",
    top_n: int = 10,
    min_quote_volume_24h: float = 0.0,
    exclude: Iterable[str] | None = None,
) -> list[Gainer]:
    """Return top N USDT-margined perpetuals by 24h percentage change.
    Uses the public fapi endpoint (no API key needed). The `client` param
    is kept for backward compatibility but is no longer required."""
    exclude_set: set[str] = set()
    for s in (exclude or []):
        if not s:
            continue
        exclude_set.add(str(s).upper())
        exclude_set.add(str(s).upper().replace("/USDT", "").replace(":USDT", "") + "USDT")

    try:
        r = requests.get(FAPI_TICKER, timeout=15)
        r.raise_for_status()
        tickers = r.json()
    except Exception as e:
        raise RuntimeError(f"fapi ticker fetch failed: {e}") from e

    candidates: list[Gainer] = []
    for t in tickers:
        sym = t.get("symbol", "")
        # Only USDT pairs
        if not sym.endswith("USDT"):
            continue
        # Exclude non-perpetual or special symbols
        if sym in ("BUSDUSDT", "BTCDOMUSDT", "USDCUSDT"):
            continue
        if sym.upper() in exclude_set:
            continue
        pct = t.get("priceChangePercent")
        qvol = float(t.get("quoteVolume", 0))
        last = t.get("lastPrice")
        if pct is None or last is None:
            continue
        pct = float(pct)
        last = float(last)
        if qvol < min_quote_volume_24h:
            continue
        # Convert to ccxt-style symbol format
        base = sym[:-4]
        ccxt_sym = f"{base}/{quote}:{quote}"
        candidates.append(Gainer(
            symbol=ccxt_sym,
            last=last,
            pct_change_24h=pct,
            quote_volume_24h=qvol,
        ))

    candidates.sort(key=lambda g: g.pct_change_24h, reverse=True)
    return candidates[:top_n]