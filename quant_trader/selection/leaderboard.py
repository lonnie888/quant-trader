"""Leaderboard utilities: aggregate results across symbols/timeframes."""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

import pandas as pd

from .ranker import rank


def build_rows(backtest_results: Iterable) -> list[dict]:
    rows = []
    for r in backtest_results:
        rows.append({
            "symbol": r.symbol,
            "timeframe": r.timeframe,
            "strategy": r.strategy,
            "params": r.params,
            "metrics": r.metrics,
            "train_metrics": r.train_metrics,
            "test_metrics": r.test_metrics,
            "equity": r.equity,
            "train_equity": r.train_equity,
            "test_equity": r.test_equity,
            "split_idx": r.split_idx,
            "trades": r.trades,
        })
    return rows


def top_by_strategy(rows: list[dict], top: int = 3) -> dict[str, list[dict]]:
    bucket: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        bucket[r["strategy"]].append(r)
    return {k: rank(v)[:top] for k, v in bucket.items()}


def to_dataframe(rows: list[dict]) -> pd.DataFrame:
    flat = []
    for r in rows:
        m = r.get("metrics", {}) or {}
        tm = r.get("train_metrics", {}) or {}
        em = r.get("test_metrics", {}) or {}
        flat.append({
            "symbol": r.get("symbol"),
            "timeframe": r.get("timeframe"),
            "strategy": r.get("strategy"),
            "params": str(r.get("params")),
            "train_return": tm.get("total_return"),
            "train_trades": tm.get("n_trades"),
            "test_return": em.get("total_return"),
            "test_sharpe": em.get("sharpe"),
            "test_dd": em.get("max_drawdown"),
            "test_wr": em.get("win_rate"),
            "test_trades": em.get("n_trades"),
            "overfit_gap": r.get("overfit_gap"),
            "score": r.get("score"),
            "passes": r.get("passes"),
        })
    return pd.DataFrame(flat)