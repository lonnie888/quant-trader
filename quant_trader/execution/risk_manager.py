"""Risk manager: enforces position sizing, daily loss limit, stop loss / take profit."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from ..backtest.broker import Position


@dataclass
class RiskConfig:
    max_position_pct: float = 0.20
    max_total_exposure: float = 0.80
    daily_loss_limit: float = 0.05
    stop_loss_pct: float = 0.03
    take_profit_pct: float = 0.06


class RiskManager:
    def __init__(self, config: RiskConfig):
        self.cfg = config
        self._day_start_equity: Optional[float] = None
        self._last_day: Optional[pd.Timestamp] = None

    def reset_day(self, ts: pd.Timestamp, equity: float):
        d = ts.normalize()
        if self._last_day != d:
            self._last_day = d
            self._day_start_equity = equity

    def daily_loss_breached(self, equity: float) -> bool:
        if self._day_start_equity is None or self._day_start_equity <= 0:
            return False
        return (equity - self._day_start_equity) / self._day_start_equity <= -abs(self.cfg.daily_loss_limit)

    def stop_loss_hit(self, pos: Position, mark: float) -> bool:
        if pos.is_flat():
            return False
        if pos.qty > 0:
            return (mark - pos.avg_price) / pos.avg_price <= -abs(self.cfg.stop_loss_pct)
        return (pos.avg_price - mark) / pos.avg_price <= -abs(self.cfg.stop_loss_pct)

    def take_profit_hit(self, pos: Position, mark: float) -> bool:
        if pos.is_flat():
            return False
        if pos.qty > 0:
            return (mark - pos.avg_price) / pos.avg_price >= abs(self.cfg.take_profit_pct)
        return (pos.avg_price - mark) / pos.avg_price >= abs(self.cfg.take_profit_pct)

    def can_open_new(self, equity: float, current_exposure_pct: float) -> bool:
        if equity <= 0:
            return False
        return current_exposure_pct + self.cfg.max_position_pct <= self.cfg.max_total_exposure
