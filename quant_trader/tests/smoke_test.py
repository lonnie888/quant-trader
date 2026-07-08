"""Smoke test: verifies project structure, config loading, scoring, and report writers
work without requiring pandas/numpy/ccxt to be installed.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Project root contains config/, data_store/, deploy/, reports/ etc.
# This file is at <root>/quant_trader/tests/smoke_test.py -> parents[2] is project root.
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PKG = ROOT / "quant_trader"
sys.path.insert(0, str(PKG))

# 1) Project structure
required_pkg = [
    "quant_trader/selection/ranker.py", "quant_trader/selection/optimizer.py", "quant_trader/selection/leaderboard.py",
    "quant_trader/strategy/base.py", "quant_trader/strategy/registry.py", "quant_trader/strategy/generator/auto_strategy.py",
    "quant_trader/strategy/library/ma_cross.py", "quant_trader/strategy/library/macd.py", "quant_trader/strategy/library/rsi.py",
    "quant_trader/strategy/library/bollinger.py", "quant_trader/strategy/library/kdj.py", "quant_trader/strategy/library/turtle.py",
    "quant_trader/strategy/library/breakout.py", "quant_trader/strategy/library/mean_reversion.py",
    "quant_trader/backtest/broker.py", "quant_trader/backtest/portfolio.py", "quant_trader/backtest/engine.py",
    "quant_trader/backtest/metrics.py", "quant_trader/backtest/report.py",
    "quant_trader/execution/paper_trader.py", "quant_trader/execution/risk_manager.py", "quant_trader/execution/notifier.py",
    "quant_trader/scripts/update_data.py", "quant_trader/scripts/run_backtest.py", "quant_trader/scripts/run_daily.py",
    "quant_trader/config.py",
]
required_root = [
    "config/settings.yaml", "config/strategies.yaml",
    "deploy/setup.sh", "deploy/run_pipeline.sh", "deploy/README.md",
    "requirements.txt", "README.md",
]
missing = [p for p in required_pkg + required_root if not (ROOT / p).exists()]
assert not missing, f"missing files: {missing}"
print(f"[OK] {len(required_pkg) + len(required_root)} required files present")

# 2) Config loading
import yaml
cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
assert cfg["backtest"]["leverage"] == 3
assert cfg["scoring"]["constraints"]["max_drawdown"] == 0.20
print("[OK] settings.yaml parsed")

# 3) Scoring
from quant_trader.selection.ranker import score, passes_constraints
good = {"total_return": 0.45, "sharpe": 1.8, "max_drawdown": -0.12, "n_trades": 80, "win_rate": 0.55, "profit_factor": 1.8}
s = score(good)
assert s > 0
assert passes_constraints(good)
bad = {**good, "sharpe": 0.5, "max_drawdown": -0.40, "n_trades": 5}
assert not passes_constraints(bad)
print(f"[OK] score(good) = {s:.4f}, constraints reject bad result")

# 4) Reports
from quant_trader.backtest.report import write_markdown, write_json
rows = [
    {"symbol": "BTC/USDT", "timeframe": "15m", "strategy": "ma_cross", "params": {"fast": 8, "slow": 34}, "metrics": good, "score": s, "passes": True},
    {"symbol": "ETH/USDT", "timeframe": "1h",  "strategy": "rsi",     "params": {"period": 14},        "metrics": bad,  "score": -1.0, "passes": False},
]
out_md = ROOT / "reports" / "smoke_leaderboard.md"
out_json = ROOT / "reports" / "smoke_leaderboard.json"
write_markdown(rows, str(out_md))
write_json(rows, str(out_json))
assert out_md.exists() and out_json.exists()
content = out_md.read_text(encoding="utf-8")
assert "BTC/USDT" in content and "ma_cross" in content
print(f"[OK] reports written, markdown contains expected rows")

# 5) Strategy config
strat_cfg = (ROOT / "config/strategies.yaml").read_text(encoding="utf-8")
for name in ["ma_cross", "macd", "rsi", "bollinger", "kdj", "turtle", "breakout", "mean_reversion"]:
    assert f"{name}:" in strat_cfg
print("[OK] all 8 strategies declared")

# 6) Optimizer
from quant_trader.selection.optimizer import grid_search
best_params, best_score = grid_search({"a": [1, 2, 3], "b": [10, 20]}, lambda p: p.get("a", 0) * 2 + p.get("b", 0))
assert best_params == {"a": 3, "b": 20} and best_score == 26
print("[OK] grid_search returns best params")

# 7) Registry
from quant_trader.strategy.registry import REGISTRY, build
assert "ma_cross" in REGISTRY and "rsi" in REGISTRY
inst = build("ma_cross", {"fast": 5, "slow": 20, "side": "long_only"})
assert inst.name == "ma_cross"
print("[OK] strategy registry builds instances")

print("\nALL SMOKE TESTS PASSED")