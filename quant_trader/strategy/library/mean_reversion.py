"""Z-score mean-reversion strategy on log returns."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Side, Strategy


class MeanReversionStrategy(Strategy):
    name = "mean_reversion"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        period = int(self.params.get("zscore_period", 30))
        z_entry = float(self.params.get("z_entry", 2.0))
        z_exit = float(self.params.get("z_exit", 0.5))
        side_mode = self.params.get("side", "long_short")
        log_p = np.log(df["close"])
        mu = log_p.rolling(period).mean()
        sd = log_p.rolling(period).std()
        z = (log_p - mu) / sd.replace(0, np.nan)
        long_entry = z < -z_entry
        short_entry = z > z_entry
        long_exit = z > -z_exit
        short_exit = z < z_exit

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
