"""Performance metrics for backtest results."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _annual_factor(idx: pd.DatetimeIndex) -> float:
    if len(idx) < 2:
        return 0.0
    delta = (idx[-1] - idx[0]).total_seconds() / max(len(idx) - 1, 1)
    seconds_per_year = 365.25 * 24 * 3600
    return seconds_per_year / max(delta, 1.0)


def compute_metrics(equity: pd.Series, trades: list | None = None) -> dict:
    if equity is None or equity.empty:
        return _empty()
    rets = equity.pct_change().dropna()
    if rets.empty:
        return _empty()
    ann = _annual_factor(equity.index)

    total_return = float(equity.iloc[-1] / equity.iloc[0] - 1.0)
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (ann / max(len(equity), 1)) - 1.0 if equity.iloc[0] > 0 else 0.0
    vol = float(rets.std() * math.sqrt(ann)) if not math.isnan(rets.std()) else 0.0
    sharpe = float(rets.mean() / rets.std() * math.sqrt(ann)) if rets.std() and not math.isnan(rets.std()) else 0.0
    downside = rets[rets < 0].std()
    sortino = float(rets.mean() / downside * math.sqrt(ann)) if downside and not math.isnan(downside) else 0.0

    running_max = equity.cummax()
    drawdown = (equity - running_max) / running_max
    max_dd = float(drawdown.min()) if not drawdown.empty else 0.0
    calmar = float(cagr / abs(max_dd)) if max_dd < 0 else 0.0

    n_trades = 0
    win_rate = 0.0
    profit_factor = 0.0
    avg_pnl = 0.0
    if trades:
        closed = [t for t in trades if getattr(t, "realized_pnl", 0) != 0 or "close" in getattr(t, "note", "")]
        pnls = [t.realized_pnl for t in closed if t.realized_pnl != 0]
        n_trades = len(pnls)
        if pnls:
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]
            win_rate = len(wins) / len(pnls)
            profit_factor = sum(wins) / abs(sum(losses)) if losses else float("inf")
            avg_pnl = float(np.mean(pnls))

    return {
        "total_return": total_return,
        "cagr": float(cagr),
        "annual_vol": vol,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_dd,
        "calmar": calmar,
        "n_trades": n_trades,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_pnl": avg_pnl,
    }


def _empty() -> dict:
    return {
        "total_return": 0.0, "cagr": 0.0, "annual_vol": 0.0,
        "sharpe": 0.0, "sortino": 0.0, "max_drawdown": 0.0,
        "calmar": 0.0, "n_trades": 0, "win_rate": 0.0,
        "profit_factor": 0.0, "avg_pnl": 0.0,
    }
