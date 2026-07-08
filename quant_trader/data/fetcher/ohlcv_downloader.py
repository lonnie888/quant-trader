"""Download OHLCV (and optional funding rate) data into a DataFrame."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd

from .binance_client import BinanceClient


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def download_ohlcv(
    client: BinanceClient,
    symbol: str,
    timeframe: str,
    lookback_days: int,
) -> pd.DataFrame:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    raw = client.fetch_ohlcv_range(symbol, timeframe, _ms(start), _ms(end))
    if not raw:
        return pd.DataFrame()
    df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df.set_index("timestamp", inplace=True)
    df = df.astype(float)
    return df


def download_funding(
    client: BinanceClient,
    symbol: str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    raw = client.fetch_funding_rate_range(symbol, _ms(start), _ms(end))
    if not raw:
        return pd.DataFrame(columns=["fundingRate"])
    rows = []
    for r in raw:
        ts = r.get("timestamp")
        rate = r.get("fundingRate")
        if ts is None or rate is None:
            continue
        rows.append({"timestamp": pd.to_datetime(ts, unit="ms", utc=True), "fundingRate": float(rate)})
    if not rows:
        return pd.DataFrame(columns=["fundingRate"])
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df


def download_many(
    client: BinanceClient,
    symbols: Iterable[str],
    timeframes: Iterable[str],
    lookback_days: int,
    include_funding: bool = False,
) -> dict[str, dict[str, pd.DataFrame]]:
    """Return {symbol: {timeframe: df}}."""
    out: dict[str, dict[str, pd.DataFrame]] = {}
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    for sym in symbols:
        per_tf: dict[str, pd.DataFrame] = {}
        for tf in timeframes:
            per_tf[tf] = download_ohlcv(client, sym, tf, lookback_days)
        if include_funding:
            per_tf["funding"] = download_funding(client, sym, start, end)
        out[sym] = per_tf
    return out
