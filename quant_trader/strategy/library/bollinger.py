"""Bollinger band mean-reversion strategy."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


class BollingerStrategy(Strategy):
    name = "bollinger"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        period = int(self.params.get("period", 20))
        std = float(self.params.get("std", 2.0))
        side_mode = self.params.get("side", "long_short")
        sma = df["close"].rolling(period).mean()
        sd = df["close"].rolling(period).std()
        upper = sma + std * sd
        lower = sma - std * sd
        long_entry = df["close"] < lower
        short_entry = df["close"] > upper
        long_exit = df["close"] >= sma
        short_exit = df["close"] <= sma

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
