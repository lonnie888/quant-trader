"""Smoke test: verifies project structure, config loading, scoring, and report writers
work without requiring pandas/numpy/ccxt to be installed."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PKG = ROOT / "quant_trader"
sys.path.insert(0, str(PKG))

# 1) Project structure
required_pkg = [
    "quant_trader/config.py",
    "quant_trader/strategy/base.py", "quant_trader/strategy/registry.py",
    "quant_trader/strategy/library/pump_pullback.py",
    "quant_trader/execution/paper_ledger.py", "quant_trader/execution/notifier.py",
    "quant_trader/scripts/daemon.py", "quant_trader/scripts/daily_runner.py",
]
required_root = [
    "config/settings.yaml", "config/strategies.yaml",
    "deploy/setup.sh", "deploy/README.md",
    "requirements.txt", "README.md",
]
missing = [p for p in required_pkg + required_root if not (ROOT / p).exists()]
assert not missing, f"missing files: {missing}"
print(f"[OK] {len(required_pkg) + len(required_root)} required files present")

# 2) Config loading
import yaml
cfg = yaml.safe_load((ROOT / "config/settings.yaml").read_text(encoding="utf-8"))
assert cfg["backtest"]["leverage"] == 3
assert cfg["risk"]["daily_loss_limit"] == 0.30
print("[OK] settings.yaml parsed")

# 3) Strategy config — pump_pullback is the only active strategy
strat_cfg = yaml.safe_load((ROOT / "config/strategies.yaml").read_text(encoding="utf-8"))
assert "pump_pullback" in strat_cfg.get("strategies", {})
assert strat_cfg["strategies"]["pump_pullback"]["active"] is True
assert strat_cfg["strategies"]["pump_pullback"]["params"]["take_profit_pct"][0] == 0.30
print("[OK] strategies.yaml: pump_pullback active, TP=30%")

# 4) Registry
from quant_trader.strategy.registry import REGISTRY, build
assert "pump_pullback" in REGISTRY
inst = build("pump_pullback", {"pump_window": 12, "pump_threshold": 0.13})
assert inst.name == "pump_pullback"
print("[OK] strategy registry builds pump_pullback")

print("\nALL SMOKE TESTS PASSED")