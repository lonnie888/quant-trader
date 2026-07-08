"""Binance USDT-margined perpetual futures client (public + signed)."""
from __future__ import annotations

import time
from typing import Any

import ccxt


def _is_placeholder(value: str) -> bool:
    if not value:
        return True
    v = value.strip().upper()
    return v.startswith("YOUR_") or v in ("", "NONE", "NULL", "REPLACE_ME")


class BinanceClient:
    def __init__(self, api_key: str = "", api_secret: str = "", testnet: bool = False):
        if _is_placeholder(api_key):
            api_key = ""
        if _is_placeholder(api_secret):
            api_secret = ""
        self.exchange = ccxt.binanceusdm({
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
        if testnet:
            self.exchange.set_sandbox_mode(True)

    # ---------- market data ----------
    def fetch_tickers(self) -> dict[str, Any]:
        return self.exchange.fetch_tickers()

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 1000) -> list[list]:
        return self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    def fetch_ohlcv_range(self, symbol: str, timeframe: str, since_ms: int, until_ms: int | None = None) -> list[list]:
        out: list[list] = []
        tf_ms = self.exchange.parse_timeframe(timeframe) * 1000
        cursor = since_ms
        while True:
            batch = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
            if not batch:
                break
            for row in batch:
                ts = row[0]
                if until_ms is not None and ts >= until_ms:
                    return out
                out.append(row)
            cursor = batch[-1][0] + tf_ms
            if len(batch) < 1000:
                break
            time.sleep(self.exchange.rateLimit / 1000)
        return out

    def fetch_funding_rate(self, symbol: str, limit: int = 1000) -> list[dict]:
        try:
            return self.exchange.fetch_funding_rate_history(symbol, limit=limit)
        except Exception:
            return []

    def fetch_funding_rate_range(self, symbol: str, start_ms: int, end_ms: int | None = None) -> list[dict]:
        """Paginated funding rate history. ccxt 4.5 uses `since` param, not `startTime`."""
        out: list[dict] = []
        cursor = start_ms
        while True:
            try:
                batch = self.exchange.fetch_funding_rate_history(symbol, since=cursor, limit=1000)
            except TypeError:
                # older ccxt used startTime
                batch = self.exchange.fetch_funding_rate_history(symbol, params={"startTime": cursor}, limit=1000)
            if not batch:
                break
            for row in batch:
                ts = row.get("timestamp")
                if ts is None:
                    dt = row.get("datetime")
                    if isinstance(dt, str):
                        from datetime import datetime
                        ts = int(datetime.fromisoformat(dt.replace("Z", "+00:00")).timestamp() * 1000)
                if end_ms is not None and ts and ts >= end_ms:
                    return out
                out.append(row)
            if len(batch) < 1000:
                break
            last = batch[-1].get("timestamp")
            if not last:
                break
            cursor = int(last) + 1
            time.sleep(self.exchange.rateLimit / 1000)
        return out

    def close(self):
        try:
            self.exchange.close()
        except Exception:
            pass