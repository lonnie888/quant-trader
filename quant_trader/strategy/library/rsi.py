"""RSI mean-reversion strategy."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Side, Strategy


class RSIStrategy(Strategy):
    name = "rsi"

    def _rsi(self, series: pd.Series, n: int) -> pd.Series:
        delta = series.diff()
        up = delta.clip(lower=0)
        down = -delta.clip(upper=0)
        roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
        roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
        rs = roll_up / roll_down.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        period = int(self.params.get("period", 14))
        ob = float(self.params.get("overbought", 70))
        os_ = float(self.params.get("oversold", 30))
        side_mode = self.params.get("side", "long_short")
        rsi = self._rsi(df["close"], period)

        out = pd.Series(Side.FLAT.value, index=df.index)
        long_entry = rsi < os_
        short_entry = rsi > ob
        long_exit = rsi > 50
        short_exit = rsi < 50

        out[long_entry] = Side.LONG.value
        out[short_entry] = Side.SHORT.value if side_mode == "long_short" else Side.FLAT.value
        # crude state machine: in long only when rsi > 50, in short only when rsi < 50
        state = pd.Series(Side.FLAT.value, index=df.index)
        cur = Side.FLAT.value
        for i, (val, le, lx, se, sx) in enumerate(zip(rsi, long_entry, long_exit, short_entry, short_exit)):
            if cur == Side.LONG.value and lx:
                cur = Side.FLAT.value
            if cur == Side.SHORT.value and sx:
                cur = Side.FLAT.value
            if cur == Side.FLAT.value:
                if le:
                    cur = Side.LONG.value
                elif se and side_mode == "long_short":
                    cur = Side.SHORT.value
            state.iloc[i] = cur
        return self._side_series(df, state).fillna(Side.FLAT.value).astype(int)
