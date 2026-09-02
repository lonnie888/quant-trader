#!/usr/bin/env bash
# Quant Trader - dev runner (linux/mac/wsl/git-bash)
# Usage:  bash dev.sh                  # smoke test only
#         bash dev.sh backtest         # smoke + backtest
#         bash dev.sh install          # install deps first
#         bash dev.sh full             # install + smoke + update + backtest
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="${VENV:-$ROOT/.venv}"
PY="$VENV/bin/python"
[ -d "$VENV" ] || { echo "[dev] creating venv at $VENV"; python3 -m venv "$VENV"; }

cmd="${1:-smoke}"
case "$cmd" in
  install)
    "$PY" -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
    "$PY" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    ;;
  smoke)
    "$PY" -m quant_trader.tests.smoke_test
    ;;
  backtest)
    "$PY" -m quant_trader.tests.smoke_test
    "$PY" -m quant_trader.scripts.run_backtest
    ;;
  update)
    "$PY" -m quant_trader.scripts.update_data
    ;;
  full)
    "$PY" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    "$PY" -m quant_trader.tests.smoke_test
    "$PY" -m quant_trader.scripts.update_data
    "$PY" -m quant_trader.scripts.run_backtest
    ;;
  *) echo "usage: bash dev.sh {install|smoke|backtest|update|full}"; exit 1 ;;
esac

echo "[dev] leaderboard: $ROOT/reports/leaderboard.md"