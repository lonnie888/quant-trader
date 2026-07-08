"""ATR-based breakout strategy with volatility filter."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(n).mean()


class BreakoutStrategy(Strategy):
    name = "breakout"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        lookback = int(self.params.get("lookback", 50))
        atr_period = int(self.params.get("atr_period", 14))
        atr_mult = float(self.params.get("atr_mult", 1.5))
        side_mode = self.params.get("side", "long_short")
        hh = df["high"].rolling(lookback).max()
        ll = df["low"].rolling(lookback).min()
        atr = _atr(df, atr_period)
        upper = hh + atr_mult * atr
        lower = ll - atr_mult * atr

        long_entry = df["close"] > upper.shift(1)
        short_entry = df["close"] < lower.shift(1)
        long_exit = df["close"] < df["close"].rolling(lookback).mean()
        short_exit = df["close"] > df["close"].rolling(lookback).mean()

        state = pd.Series(0, index=df.index)
        cur = 0
        for i, (le, lx, se, sx) in enumerate(zip(long_entry, long_exit, short_entry, short_exit)):
            if cur == 1 and lx:
                cur = 0
            if cur == -1 and sx:
                cur = 0
            if cur == 0:
                if le:
                    cur = 1
                elif se and side_mode == "long_short":
                    cur = -1
            state.iloc[i] = cur
        return self._side_series(df, state).fillna(0).astype(int)
