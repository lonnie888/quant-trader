"""Portfolio: aggregates positions, tracks equity curve."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .broker import Broker


@dataclass
class Portfolio:
    initial_capital: float = 10_000.0
    cash: float = 0.0
    broker: Broker = field(default_factory=Broker)
    history: list[dict] = field(default_factory=list)

    def reset(self):
        self.cash = self.initial_capital
        self.broker = Broker(fee_rate=self.broker.fee_rate, slippage_bps=self.broker.slippage_bps, use_funding=self.broker.use_funding)
        self.history.clear()

    def equity(self, mark_prices: dict[str, float]) -> float:
        upnl = sum(self.broker.mark_to_market(mark_prices).values())
        return self.cash + upnl

    def step(self, ts: pd.Timestamp, mark_prices: dict[str, float]):
        # Aggregate realized PnL and funding carry across all positions.
        realized = sum(p.realized_pnl for p in self.broker.positions.values())
        funding = sum(p.funding_paid for p in self.broker.positions.values())
        fees = sum(sum(f.fee for f in p.trades) for p in self.broker.positions.values())
        # cash = initial + realized - fees + funding (USDT-margined linear contract)
        self.cash = self.initial_capital + realized + funding - fees
        eq = self.equity(mark_prices)
        self.history.append({
            "timestamp": ts,
            "equity": eq,
            "cash": self.cash,
            "unrealized": eq - self.cash,
            "realized": realized,
            "funding_paid": funding,
            "fees": fees,
        })

    def equity_series(self) -> pd.Series:
        if not self.history:
            return pd.Series(dtype=float)
        df = pd.DataFrame(self.history).set_index("timestamp")
        return df["equity"].astype(float)