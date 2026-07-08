"""Backtest report writer (Markdown / JSON)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


def _fmt(x, pct: bool = False) -> str:
    if x is None:
        return "-"
    if isinstance(x, float) and (x != x):  # NaN
        return "-"
    if pct:
        return f"{x*100:.2f}%"
    if isinstance(x, float):
        return f"{x:.4f}"
    return str(x)


def write_markdown(rows: list[dict], out_path: str, title: str = "Backtest Leaderboard") -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    headers = [
        "rank", "symbol", "timeframe", "strategy", "params",
        "total_return", "sharpe", "max_drawdown", "win_rate",
        "profit_factor", "n_trades", "score",
    ]
    lines = [f"# {title}", "", f"_Generated at {datetime.utcnow().isoformat()}Z_", ""]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
    for i, r in enumerate(rows, 1):
        params = r.get("params", {})
        pstr = ", ".join(f"{k}={v}" for k, v in params.items()) if isinstance(params, dict) else str(params)
        m = r.get("metrics", {})
        lines.append("| " + " | ".join([
            str(i),
            str(r.get("symbol", "")),
            str(r.get("timeframe", "")),
            str(r.get("strategy", "")),
            pstr,
            _fmt(m.get("total_return"), pct=True),
            _fmt(m.get("sharpe")),
            _fmt(m.get("max_drawdown"), pct=True),
            _fmt(m.get("win_rate"), pct=True),
            _fmt(m.get("profit_factor")),
            str(m.get("n_trades", 0)),
            _fmt(r.get("score")),
        ]) + " |")
    Path(out_path).write_text("\n".join(lines), encoding="utf-8")
    return out_path


def write_json(rows: list[dict], out_path: str) -> str:
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_text(json.dumps(rows, indent=2, default=str), encoding="utf-8")
    return out_path
