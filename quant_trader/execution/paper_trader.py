"""Paper trader: applies the best backtest strategy to live kline feed (no real orders)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

import pandas as pd

from ..backtest.broker import Broker
from ..backtest.portfolio import Portfolio
from .risk_manager import RiskConfig, RiskManager

log = logging.getLogger(__name__)


@dataclass
class PaperTrader:
    broker: Broker = field(default_factory=Broker)
    portfolio: Portfolio = field(default_factory=Portfolio)
    risk: RiskManager = field(default_factory=lambda: RiskManager(RiskConfig()))

    def __post_init__(self):
        self.portfolio.broker = self.broker

    def on_bar(self, ts: pd.Timestamp, symbol: str, price: float, target_side: int, funding_rate: float = 0.0):
        self.risk.reset_day(ts, self.portfolio.equity({symbol: price}))

        pos = self.broker.positions.get(symbol)
        if pos and not pos.is_flat():
            if self.risk.stop_loss_hit(pos, price):
                log.info("stop loss triggered for %s @ %s", symbol, price)
                self.broker.submit(ts, symbol, 0, price, 0, funding_rate=funding_rate, note="stop_loss")
            elif self.risk.take_profit_hit(pos, price):
                log.info("take profit triggered for %s @ %s", symbol, price)
                self.broker.submit(ts, symbol, 0, price, 0, funding_rate=funding_rate, note="take_profit")
            else:
                self.broker.submit(ts, symbol, target_side, price, 0, funding_rate=funding_rate)
        else:
            if not self.risk.daily_loss_breached(self.portfolio.equity({symbol: price})):
                self.broker.submit(ts, symbol, target_side, price, 0, funding_rate=funding_rate)

        self.portfolio.cash = self.portfolio.initial_capital
        self.portfolio.step(ts, {symbol: price})
