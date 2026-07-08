"""Event-driven backtest engine (single-symbol)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .broker import Broker
from .portfolio import Portfolio


@dataclass
class BacktestResult:
    symbol: str
    timeframe: str
    strategy: str
    params: dict
    metrics: dict
    train_metrics: dict = field(default_factory=dict)
    test_metrics: dict = field(default_factory=dict)
    equity: pd.Series = field(default_factory=pd.Series)
    train_equity: pd.Series = field(default_factory=pd.Series)
    test_equity: pd.Series = field(default_factory=pd.Series)
    split_idx: Optional[pd.Timestamp] = None
    trades: list = field(default_factory=list)


def run_backtest(
    df: pd.DataFrame,
    signals: pd.Series,
    *,
    symbol: str,
    timeframe: str,
    strategy_name: str,
    params: dict,
    initial_capital: float = 10_000.0,
    leverage: float = 3.0,
    fee_rate: float = 0.0004,
    slippage_bps: float = 5.0,
    use_funding: bool = True,
    funding_col: str | None = "funding_rate",
    risk_per_trade: float = 0.10,
    train_test_split: float = 0.0,  # 0 = no split, 0.7 = 70% train / 30% test
) -> BacktestResult:
    """Run a backtest on a single OHLCV dataframe.

    If `train_test_split` > 0, equity is split at that fraction and `train_metrics`
    / `test_metrics` are computed on each segment separately.
    """
    broker = Broker(fee_rate=fee_rate, slippage_bps=slippage_bps, use_funding=use_funding)
    pf = Portfolio(initial_capital=initial_capital, broker=broker)
    pf.cash = initial_capital

    df = df.copy()
    sig = signals.reindex(df.index).fillna(0).astype(int)
    if funding_col and funding_col in df.columns:
        fr = df[funding_col].fillna(0.0)
    else:
        fr = pd.Series(0.0, index=df.index)

    closes = df["close"].values
    sigs = sig.values
    frs = fr.values
    idx = df.index

    base_notional_cap = initial_capital * leverage * risk_per_trade

    for i, ts in enumerate(idx):
        price = float(closes[i])
        target = int(sigs[i])
        mark_prices = {symbol: price}
        if use_funding and broker.positions.get(symbol) is not None and not broker.positions[symbol].is_flat():
            pos = broker.positions[symbol]
            broker.apply_funding(ts, symbol, float(frs[i]), price, abs(pos.qty) * price)

        eq = pf.equity(mark_prices)
        if target != 0 and eq > 0:
            qty = base_notional_cap / max(price, 1e-12)
            broker.submit(
                ts=ts, symbol=symbol, target_side=target, price=price, qty=qty,
                leverage=leverage, funding_rate=float(frs[i]),
            )
        else:
            broker.submit(
                ts=ts, symbol=symbol, target_side=0, price=price, qty=0,
                leverage=leverage, funding_rate=float(frs[i]),
            )

        pf.step(ts, mark_prices)

    # force-close any open position at last bar
    pos = broker.positions.get(symbol)
    if pos and not pos.is_flat():
        last_ts = idx[-1]
        last_px = float(closes[-1])
        broker.submit(last_ts, symbol, 0, last_px, 0, leverage=leverage, funding_rate=0.0, note="force_close")
        pf.step(last_ts, {symbol: last_px})

    equity = pf.equity_series()
    from .metrics import compute_metrics
    metrics = compute_metrics(equity, broker.fills)

    train_metrics: dict = {}
    test_metrics: dict = {}
    train_equity = pd.Series(dtype=float)
    test_equity = pd.Series(dtype=float)
    split_idx: Optional[pd.Timestamp] = None

    if 0.0 < train_test_split < 1.0 and not equity.empty:
        cut = int(len(equity) * train_test_split)
        if 0 < cut < len(equity):
            train_equity = equity.iloc[:cut]
            test_equity = equity.iloc[cut:]
            split_idx = equity.index[cut]
            # restrict trades to each segment for accurate n_trades / win_rate
            train_fills = [t for t in broker.fills if t.timestamp <= split_idx]
            test_fills = [t for t in broker.fills if t.timestamp > split_idx]
            train_metrics = compute_metrics(train_equity, train_fills)
            test_metrics = compute_metrics(test_equity, test_fills)

    return BacktestResult(
        symbol=symbol, timeframe=timeframe, strategy=strategy_name,
        params=params, metrics=metrics,
        train_metrics=train_metrics, test_metrics=test_metrics,
        equity=equity, train_equity=train_equity, test_equity=test_equity,
        split_idx=split_idx, trades=broker.fills,
    )