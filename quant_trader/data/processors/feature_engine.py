"""Feature engineering on OHLCV dataframes."""
from __future__ import annotations

import numpy as np
import pandas as pd


def add_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["return_1"] = out["close"].pct_change()
    out["log_return"] = np.log(out["close"]).diff()
    out["sma_20"] = out["close"].rolling(20).mean()
    out["sma_60"] = out["close"].rolling(60).mean()
    out["ema_20"] = out["close"].ewm(span=20, adjust=False).mean()
    out["std_20"] = out["close"].rolling(20).std()
    out["atr_14"] = _atr(out, 14)
    out["rsi_14"] = _rsi(out["close"], 14)
    macd, signal, hist = _macd(out["close"])
    out["macd"] = macd
    out["macd_signal"] = signal
    out["macd_hist"] = hist
    return out


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def _rsi(series: pd.Series, n: int) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0)
    down = -delta.clip(upper=0)
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def align_funding(ohlcv: pd.DataFrame, funding: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Forward-fill funding rate to OHLCV index. Handles 'fundingRate' or 'funding_rate' columns."""
    if funding is None or funding.empty:
        out = ohlcv.copy()
        out["funding_rate"] = 0.0
        return out
    # normalize column name
    f = funding.copy()
    if "funding_rate" not in f.columns and "fundingRate" in f.columns:
        f = f.rename(columns={"fundingRate": "funding_rate"})
    if "funding_rate" not in f.columns:
        # try first numeric column
        for c in f.columns:
            if pd.api.types.is_numeric_dtype(f[c]):
                f = f.rename(columns={c: "funding_rate"})
                break
        else:
            out = ohlcv.copy()
            out["funding_rate"] = 0.0
            return out
    f = f[["funding_rate"]]
    f = f.reindex(ohlcv.index, method="ffill").fillna(0.0)
    out = ohlcv.copy()
    out["funding_rate"] = f["funding_rate"]
    return out