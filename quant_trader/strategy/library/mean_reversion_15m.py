"""MeanReversion15m 策略 - 短线均值回归 (Jesse 移植版).

逻辑:
- 超卖反弹: RSI < 阈值 且 close 曾跌破布林下轨, 现在收回带内 (反弹启动)
- 出场: 固定 SL / TP / 持有超时
- 与 Jesse MeanReversion15m 参数一致 (2026 跨币验证: RSI30/SL2.5%/TP4%/HOLD32)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..base import Side, Strategy


class MeanReversion15mStrategy(Strategy):
    name = "mean_reversion_15m"

    def generate_signals(self, df: pd.DataFrame) -> pd.Series:
        p = self.params
        bb_period = int(p.get("bb_period", 20))
        bb_std = float(p.get("bb_std", 2.0))
        rsi_period = int(p.get("rsi_period", 14))
        rsi_oversold = float(p.get("rsi_oversold", 30))
        stop_loss_pct = float(p.get("stop_loss_pct", 0.025))
        take_profit_pct = float(p.get("take_profit_pct", 0.04))
        hold_bars = int(p.get("hold_bars", 32))
        cooldown = int(p.get("cooldown", 4))

        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        n = len(df)
        if n < max(bb_period, rsi_period) + 3:
            return pd.Series(0, index=df.index)

        # RSI (Wilder)
        delta = pd.Series(close).diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / rsi_period, min_periods=rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi = rsi.fillna(50)

        # 布林带
        mid = pd.Series(close).rolling(bb_period).mean()
        std = pd.Series(close).rolling(bb_period).std()
        upper = mid + bb_std * std
        lower = mid - bb_std * std

        state = np.zeros(n, dtype=int)
        cur = 0
        held = 0
        bars_since_exit = cooldown
        entry_price = 0.0

        for i in range(n):
            if cur == 0:
                # 冷却
                if bars_since_exit < cooldown:
                    bars_since_exit += 1
                    continue
                # 超卖 + 曾破下轨 + 收回带内
                if i < 1 or np.isnan(rsi[i]) or np.isnan(lower[i]) or np.isnan(lower[i - 1]):
                    bars_since_exit += 1
                    continue
                if rsi[i] > rsi_oversold:
                    bars_since_exit += 1
                    continue
                prev_close = close[i - 1]
                prev_lower = lower[i - 1]
                if not (prev_close <= prev_lower and close[i] > lower[i]):
                    bars_since_exit += 1
                    continue
                # 开多
                cur = Side.LONG.value
                held = hold_bars
                bars_since_exit = 0
                entry_price = close[i]
                state[i] = cur
            else:
                # 持仓: SL/TP/超时
                if entry_price > 0:
                    if low[i] <= entry_price * (1 - stop_loss_pct):
                        cur = 0
                        held = 0
                        bars_since_exit = 0
                        continue
                    if take_profit_pct > 0 and high[i] >= entry_price * (1 + take_profit_pct):
                        cur = 0
                        held = 0
                        bars_since_exit = 0
                        continue
                held -= 1
                if held <= 0:
                    cur = 0
                    bars_since_exit = 0
                state[i] = cur

        return pd.Series(state, index=df.index).astype(int)
