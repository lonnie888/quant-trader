"""Pump detection + pullback entry strategy - Opt 修复版.

从 Jesse PumpPullbackOpt 迁移 (生产配置 2.1, 1H timeframe).

与原始 pump_pullback 的差异 (修复版):
1. pump 检测: 每次空仓时用最近 pump_window 根 K 线重新检测, 检测到就立即更新
   (移除错误的 local_idx > pump_bar_idx 全局索引判断, 那是原始版只保留一次泵的 bug)
2. pump_age: 用相对计数 (距最近 pump 的 bar 数), 而非全局索引
3. 参数来自 Jesse Opt: pump_window=12, threshold=0.070738 等
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Side, Strategy


class PumpPullbackOptStrategy(Strategy):
    name = "pump_pullback_opt"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        pump_window = int(p.get("pump_window", 12))
        pump_threshold = float(p.get("pump_threshold", 0.070738))
        pullback_min = float(p.get("pullback_min", 0.071613))
        pullback_max = float(p.get("pullback_max", 0.237939))
        vol_shrink = float(p.get("vol_shrink", 0.873908))
        vol_recover = float(p.get("vol_recover", 1.0))
        trigger_pct = float(p.get("trigger_pct", 0.0))
        ema_period = int(p.get("ema_period", 12))
        hold_bars = int(p.get("hold_bars", 66))
        cooldown = int(p.get("cooldown", 19))
        pump_lookback = int(p.get("pump_lookback", 96))
        stop_loss_pct = float(p.get("stop_loss_pct", 0.139842))
        take_profit_pct = float(p.get("take_profit_pct", 0.214923))

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
        # 状态: 用相对 pump_age (Opt 修复版)
        pump_high = 0.0
        pump_age = 9999
        pump_vol = 0.0
        entry_price = 0.0

        for i in range(n):
            if cur == 0:
                # ---- 泵检测 (每次空仓都重新检测最近的窗口, Opt 修复版) ----
                if i >= pump_window:
                    win_high = high[i - pump_window + 1: i + 1].max()
                    base_close = close[i - pump_window + 1]  # 窗口起点收盘 (win[0])
                    if base_close > 0 and (win_high / base_close - 1.0) >= pump_threshold:
                        # 检测到新泵, 立即更新 (覆盖旧泵)
                        pump_high = win_high
                        pump_age = 0
                        pump_vol = vol[i - pump_window + 1: i + 1].max()
                    else:
                        # 没有新泵, 泵年龄增长
                        pump_age += 1

                # 条件2: 泵信号有效期内
                if pump_age > pump_lookback:
                    state[i] = 0
                    bars_since_exit += 1
                    continue

                # 条件3: 回撤区间
                if pump_high <= 0:
                    state[i] = 0
                    bars_since_exit += 1
                    continue
                retr = 1.0 - close[i] / pump_high
                if not (pullback_min <= retr <= pullback_max):
                    state[i] = 0
                    bars_since_exit += 1
                    continue

                # 条件4: 缩量回调 (最近4根均量 < vol_shrink * pump_vol)
                recent_vol = vol[max(0, i - 3): i + 1].mean()
                if pump_vol > 0 and recent_vol > vol_shrink * pump_vol:
                    state[i] = 0
                    bars_since_exit += 1
                    continue

                # 条件5: EMA 趋势确认 (trigger_pct<=0 时要求 close > EMA)
                if trigger_pct > 0:
                    if close[i] < ema[i] * (1 + trigger_pct):
                        state[i] = 0
                        bars_since_exit += 1
                        continue
                else:
                    if close[i] <= ema[i]:
                        state[i] = 0
                        bars_since_exit += 1
                        continue

                # 条件6: 二次放量 (vol_recover>1 才检查)
                if vol_recover > 1.0:
                    short_vol = vol[max(0, i - 6): i].mean()  # 最近6根(不含当前)
                    if short_vol < vol_recover * recent_vol:
                        state[i] = 0
                        bars_since_exit += 1
                        continue

                # 条件7: 冷却期
                if bars_since_exit < cooldown:
                    state[i] = 0
                    bars_since_exit += 1
                    continue

                # 所有条件满足 -> 入场
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
