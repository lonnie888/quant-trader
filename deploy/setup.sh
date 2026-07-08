#!/usr/bin/env bash
# One-shot setup for Quant Trader on a Linux/ARM server.
# Usage:
#   bash deploy/setup.sh           # full setup + smoke test
#   bash deploy/setup.sh --no-test # skip smoke test
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

VENV_DIR="${VENV_DIR:-$PROJECT_ROOT/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
REQUIREMENTS="$PROJECT_ROOT/requirements.txt"
PIP_MIRROR="${PIP_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

log()  { printf "\033[1;36m[setup]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[setup]\033[0m %s\n" "$*"; }
fail() { printf "\033[1;31m[setup]\033[0m %s\n" "$*"; exit 1; }

# ---- 0. 环境检查 ----
log "checking environment..."
command -v "$PYTHON_BIN" >/dev/null 2>&1 || fail "$PYTHON_BIN not found"
"$PYTHON_BIN" --version

# ---- 1. 建/复用 venv ----
if [ ! -d "$VENV_DIR" ]; then
  log "creating venv at $VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "venv creation failed"
else
  log "reusing existing venv at $VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip wheel setuptools --quiet -i "$PIP_MIRROR"

# ---- 2. 装依赖 ----
log "installing requirements (mirror: $PIP_MIRROR)"
python -m pip install -r "$REQUIREMENTS" -i "$PIP_MIRROR" || fail "pip install failed"

# ---- 3. 编辑配置（提示用户填 API key）----
SETTINGS="$PROJECT_ROOT/config/settings.yaml"
if grep -q "YOUR_API_KEY" "$SETTINGS"; then
  warn "$SETTINGS still contains placeholder API keys."
  warn "edit it before running update_data. (回测可不填, 拉数据需要 public read 权限)"
fi

# ---- 4. 烟测 ----
if [ "${1:-}" != "--no-test" ]; then
  log "running stage-1 smoke test (pure stdlib)..."
  python -m quant_trader.tests.smoke_test
  log "smoke test passed"
fi

log "DONE. Next steps:"
echo "  source $VENV_DIR/bin/activate"
echo "  cd $PROJECT_ROOT"
echo "  python -m quant_trader.scripts.update_data        # 拉涨幅榜 + K线"
echo "  python -m quant_trader.scripts.run_backtest      # 跑回测 + 出报告"
echo "  python -m quant_trader.scripts.run_daily          # 一条龙"
