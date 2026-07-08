"""Composite scoring for selecting the best strategy + parameter combo."""
from __future__ import annotations

import math

DEFAULT_WEIGHTS = {
    "weight_return": 1.0,
    "weight_sharpe": 0.5,
    "weight_winrate": 0.3,
    "weight_drawdown": 2.0,
    "weight_profit_factor": 0.5,
}

DEFAULT_CONSTRAINTS = {
    "max_drawdown": 0.20,
    "min_sharpe": 1.0,
    "min_trades": 30,
}


def passes_constraints(metrics: dict, constraints: dict | None = None) -> bool:
    c = {**DEFAULT_CONSTRAINTS, **(constraints or {})}
    if metrics.get("n_trades", 0) < c["min_trades"]:
        return False
    if metrics.get("sharpe", 0.0) < c["min_sharpe"]:
        return False
    if metrics.get("max_drawdown", 0.0) < -abs(c["max_drawdown"]):
        return False
    return True


def score(metrics: dict, weights: dict | None = None) -> float:
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    pf = metrics.get("profit_factor", 0.0)
    if pf == float("inf"):
        pf = 5.0
    s = (
        w["weight_return"] * metrics.get("total_return", 0.0)
        + w["weight_sharpe"] * metrics.get("sharpe", 0.0)
        + w["weight_winrate"] * metrics.get("win_rate", 0.0)
        - w["weight_drawdown"] * abs(metrics.get("max_drawdown", 0.0))
        + w["weight_profit_factor"] * (pf - 1.0)
    )
    return float(s)


def rank(rows: list[dict], weights: dict | None = None, constraints: dict | None = None) -> list[dict]:
    """Annotate rows with `score` and `passes`, return sorted desc by score."""
    out = []
    for r in rows:
        m = r.get("metrics", {}) or {}
        s = score(m, weights)
        passes = passes_constraints(m, constraints)
        nr = {**r, "score": s, "passes": passes}
        out.append(nr)
    out.sort(key=lambda x: x.get("score", -1e18), reverse=True)
    return out
