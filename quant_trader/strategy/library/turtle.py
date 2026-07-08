"""Turtle trading: Donchian channel breakout with stop-loss on N-bar low."""
from __future__ import annotations

import pandas as pd

from ..base import Side, Strategy


class TurtleStrategy(Strategy):
    name = "turtle"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        entry = int(self.params.get("entry", 20))
        exit_ = int(self.params.get("exit", 10))
        side_mode = self.params.get("side", "long_short")
        hh = df["high"].rolling(entry).max()
        ll = df["low"].rolling(entry).min()
        hh_exit = df["high"].rolling(exit_).max()
        ll_exit = df["low"].rolling(exit_).min()

        long_entry = df["close"] > hh.shift(1)
        short_entry = df["close"] < ll.shift(1)
        long_exit = df["close"] < ll_exit.shift(1)
        short_exit = df["close"] > hh_exit.shift(1)

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
