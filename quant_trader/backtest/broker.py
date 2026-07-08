"""Simulated broker for USDT-margined perpetual futures.

- Linear contract: PnL in quote (USDT)
- Fees charged on notional entry/exit
- Optional funding-rate carry applied on each funding timestamp
- Slippage modeled in basis points against the fill price
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import pandas as pd


@dataclass
class Fill:
    timestamp: pd.Timestamp
    symbol: str
    side: int                # +1 buy, -1 sell
    qty: float
    price: float
    fee: float
    realized_pnl: float
    funding_pnl: float = 0.0
    note: str = ""


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    entry_ts: Optional[pd.Timestamp] = None
    realized_pnl: float = 0.0
    funding_paid: float = 0.0
    trades: list[Fill] = field(default_factory=list)

    def is_flat(self) -> bool:
        return abs(self.qty) < 1e-12

    def unrealized(self, mark: float, leverage: float) -> float:
        return (mark - self.avg_price) * self.qty


class Broker:
    def __init__(
        self,
        fee_rate: float = 0.0004,
        slippage_bps: float = 5.0,
        use_funding: bool = True,
    ):
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.use_funding = use_funding
        self.positions: dict[str, Position] = {}
        self.fills: list[Fill] = []

    def _slip(self, price: float, side: int) -> float:
        bps = self.slippage_bps / 10_000.0
        return price * (1 + bps * side)

    def apply_funding(self, ts: pd.Timestamp, symbol: str, funding_rate: float, mark_price: float, qty_notional: float) -> float:
        if not self.use_funding or qty_notional == 0:
            return 0.0
        # funding paid by longs to shorts when rate > 0
        pos = self.positions.get(symbol)
        if pos is None or pos.is_flat():
            return 0.0
        # qty in base = position.qty; notional = abs(qty)*mark
        notional = abs(pos.qty) * mark_price
        payment = -funding_rate * notional * (1 if pos.qty > 0 else -1)
        pos.funding_paid += payment
        return payment

    def submit(
        self,
        ts: pd.Timestamp,
        symbol: str,
        target_side: int,        # -1, 0, +1
        price: float,
        qty: float,
        leverage: float = 1.0,
        funding_rate: float = 0.0,
        note: str = "",
    ) -> Optional[Fill]:
        """Adjust position toward `target_side`. Returns fill or None if no change."""
        pos = self.positions.setdefault(symbol, Position(symbol=symbol))
        current = 1 if pos.qty > 0 else (-1 if pos.qty < 0 else 0)

        # funding carry on existing position at this bar
        if self.use_funding and funding_rate:
            self.apply_funding(ts, symbol, funding_rate, price, abs(pos.qty) * price)

        if target_side == current:
            return None

        # close or flip in a single fill, sized to `qty` for entries
        if target_side == 0:
            close_qty = -pos.qty
            return self._execute(ts, symbol, close_qty, price, note="close")
        if current == 0:
            return self._execute(ts, symbol, target_side * qty, price, note="open")
        # flip
        close_qty = -pos.qty
        flip_extra = target_side * qty
        self._execute(ts, symbol, close_qty, price, note="close")
        return self._execute(ts, symbol, flip_extra, price, note="open")

    def _execute(self, ts: pd.Timestamp, symbol: str, delta_qty: float, price: float, note: str) -> Fill:
        pos = self.positions[symbol]
        side = 1 if delta_qty > 0 else -1
        fill_price = self._slip(price, side)
        notional = abs(delta_qty) * fill_price
        fee = notional * self.fee_rate

        realized = 0.0
        if not pos.is_flat() and (pos.qty * delta_qty < 0):
            # closing (or partially closing) some of existing position
            closing_qty = min(abs(delta_qty), abs(pos.qty))
            direction = 1 if pos.qty > 0 else -1
            realized = (fill_price - pos.avg_price) * direction * closing_qty
            pos.realized_pnl += realized
            # reduce position
            new_qty = pos.qty + delta_qty
            if abs(new_qty) < 1e-12:
                pos.qty = 0.0
                pos.avg_price = 0.0
                pos.entry_ts = None
            else:
                pos.qty = new_qty
        else:
            # opening or adding
            new_qty = pos.qty + delta_qty
            if pos.is_flat() or pos.qty * delta_qty > 0:
                # update weighted avg price on adds/open
                total_cost = pos.avg_price * abs(pos.qty) + fill_price * abs(delta_qty)
                pos.avg_price = total_cost / max(abs(new_qty), 1e-12)
                if pos.entry_ts is None:
                    pos.entry_ts = ts
            pos.qty = new_qty

        fill = Fill(
            timestamp=ts, symbol=symbol, side=side, qty=abs(delta_qty),
            price=fill_price, fee=fee, realized_pnl=realized, note=note,
        )
        pos.trades.append(fill)
        self.fills.append(fill)
        return fill

    def mark_to_market(self, mark_prices: dict[str, float]) -> dict[str, float]:
        out = {}
        for sym, pos in self.positions.items():
            m = mark_prices.get(sym)
            if m is None:
                continue
            out[sym] = pos.unrealized(m, leverage=1.0)
        return out
