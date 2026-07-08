"""Moving average crossover strategy."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


class MACross(Strategy):
    name = "ma_cross"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = int(self.params.get("fast", 8))
        slow = int(self.params.get("slow", 34))
        side_mode = self.params.get("side", "long_short")
        sma_fast = df["close"].rolling(fast).mean()
        sma_slow = df["close"].rolling(slow).mean()
        cross_up = (sma_fast > sma_slow) & (sma_fast.shift(1) <= sma_slow.shift(1))
        cross_dn = (sma_fast < sma_slow) & (sma_fast.shift(1) >= sma_slow.shift(1))

        out = pd.Series(Side.FLAT.value, index=df.index)
        out[cross_up] = Side.LONG.value
        out[cross_dn] = Side.SHORT.value if side_mode == "long_short" else Side.FLAT.value
        return self._side_series(df, out).ffill().fillna(Side.FLAT.value).astype(int)
