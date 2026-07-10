"""Pump detection + pullback entry strategy.

Designed for the pattern observed on Binance USDT-perp:
    long quiet period -> sudden pump -> pullback -> second leg up

Entry requires ALL of:
  1. A pump was detected within the last `pump_lookback` bars (price rose >= pump_threshold
     over pump_window bars)
  2. A pullback: current retracement from pump high is in [pullback_min, pullback_max]
  3. Pullback volume is below `vol_shrink` x the pump volume
  4. Second-leg trigger: close > EMA(ema_period) by `trigger_pct`
     and recent volume expands to >= `vol_recover` x the pullback average volume
  5. Cooldown from previous exit (not entry) >= `cooldown` bars

While in a position:
  - bar-internal stop loss: if `low <= entry * (1 - stop_loss_pct)` -> exit at SL price
  - bar-internal take profit: if `high >= entry * (1 + take_profit_pct)` -> exit at TP price
  - SL is checked before TP (so a wick hitting both still counts as a stop)
  - on a normal close, hold for `hold_bars` then exit
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Side, Strategy


class PumpPullbackStrategy(Strategy):
    name = "pump_pullback"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        pump_window = int(p.get("pump_window", 8))
        pump_threshold = float(p.get("pump_threshold", 0.15))
        pullback_min = float(p.get("pullback_min", 0.10))
        pullback_max = float(p.get("pullback_max", 0.55))
        vol_shrink = float(p.get("vol_shrink", 0.80))
        vol_recover = float(p.get("vol_recover", 1.2))
        trigger_pct = float(p.get("trigger_pct", 0.005))
        pump_lookback = int(p.get("pump_lookback", 96))
        cooldown = int(p.get("cooldown", 16))
        ema_period = int(p.get("ema_period", 9))
        hold_bars = int(p.get("hold_bars", 24))
        stop_loss_pct = float(p.get("stop_loss_pct", 0.10))
        take_profit_pct = float(p.get("take_profit_pct", 0.0))

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        vol = df["volume"].values
        n = len(df)

        if n < max(pump_window + ema_period, 20):
            return pd.Series(0, index=df.index)

        ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values

        state = np.zeros(n, dtype=int)
        cur = 0
        held = 0
        bars_since_exit = cooldown
        pump_high = 0.0
        pump_bar_idx = -1
        pump_vol = 0.0
        entry_price = 0.0

        for i in range(n):
            if cur == 0:
                if i >= pump_window:
                    win_high = high[i - pump_window + 1: i + 1].max()
                    win_low = low[i - pump_window + 1: i + 1].min()
                    if win_low > 0 and (win_high / win_low - 1.0) >= pump_threshold:
                        local_idx = i - pump_window + 1 + int(np.argmax(high[i - pump_window + 1: i + 1]))
                        if local_idx > pump_bar_idx:
                            pump_high = win_high
                            pump_bar_idx = local_idx
                            pump_vol = vol[i - pump_window + 1: i + 1].max()

                if pump_bar_idx < 0 or i - pump_bar_idx > pump_lookback:
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                retr = 1.0 - close[i] / pump_high if pump_high > 0 else 0.0
                if not (pullback_min <= retr <= pullback_max):
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                recent_vol = vol[max(0, i - 3): i + 1].mean()
                if pump_vol > 0 and recent_vol > vol_shrink * pump_vol:
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                if close[i] < ema[i] * (1 + trigger_pct):
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                short_avg = vol[max(0, i - 6): i + 1].mean()
                if short_avg < vol_recover * recent_vol:
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                if bars_since_exit < cooldown:
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                cur = Side.LONG.value
                held = hold_bars
                bars_since_exit = 0
                entry_price = close[i]
                state[i] = cur
            else:
                # in position: bar-internal SL then TP
                if entry_price > 0:
                    if low[i] <= entry_price * (1 - stop_loss_pct):
                        cur = 0
                        held = 0
                        bars_since_exit = 0
                        state[i] = 0
                        continue
                    if take_profit_pct > 0 and high[i] >= entry_price * (1 + take_profit_pct):
                        cur = 0
                        held = 0
                        bars_since_exit = 0
                        state[i] = 0
                        continue
                held -= 1
                if held <= 0:
                    cur = 0
                    bars_since_exit = 0
                state[i] = cur

        return pd.Series(state, index=df.index).astype(int)