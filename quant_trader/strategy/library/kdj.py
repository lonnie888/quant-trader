"""KDJ strategy (KD with J line)."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


class KDJStrategy(Strategy):
    name = "kdj"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        n = int(self.params.get("n", 9))
        k_p = int(self.params.get("k_period", 3))
        d_p = int(self.params.get("d_period", 3))
        side_mode = self.params.get("side", "long_short")
        low_n = df["low"].rolling(n).min()
        high_n = df["high"].rolling(n).max()
        rsv = (df["close"] - low_n) / (high_n - low_n).replace(0, 1e-12) * 100
        k = rsv.ewm(alpha=1 / k_p, adjust=False).mean()
        d = k.ewm(alpha=1 / d_p, adjust=False).mean()
        j = 3 * k - 2 * d

        long_entry = (j < 0) | ((k > d) & (k.shift(1) <= d.shift(1)))
        short_entry = (j > 100) | ((k < d) & (k.shift(1) >= d.shift(1)))
        long_exit = k > 80
        short_exit = k < 20

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
