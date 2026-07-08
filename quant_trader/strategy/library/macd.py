"""MACD strategy."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


def _macd(series: pd.Series, fast: int, slow: int, signal: int):
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig


class MACDStrategy(Strategy):
    name = "macd"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        fast = int(self.params.get("fast", 12))
        slow = int(self.params.get("slow", 26))
        signal = int(self.params.get("signal", 9))
        side_mode = self.params.get("side", "long_short")
        macd, sig = _macd(df["close"], fast, slow, signal)
        hist = macd - sig
        cross_up = (hist > 0) & (hist.shift(1) <= 0)
        cross_dn = (hist < 0) & (hist.shift(1) >= 0)

        out = pd.Series(Side.FLAT.value, index=df.index)
        out[cross_up] = Side.LONG.value
        out[cross_dn] = Side.SHORT.value if side_mode == "long_short" else Side.FLAT.value
        return self._side_series(df, out).ffill().fillna(Side.FLAT.value).astype(int)
