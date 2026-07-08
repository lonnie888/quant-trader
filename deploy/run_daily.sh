#!/usr/bin/env bash
# run_daily.sh - the production loop.
#
# Runs once per day (typically via cron at 02:00 UTC after Binance's 24h
# gainers list has settled). Each invocation:
#   1. refreshes the last 7 days of 15m klines for the top gainers
#   2. scans + applies the locked strategy + risk gate + writes the ledger
#   3. checks all open positions against the latest bar (SL/TP/time exit)
#   4. emits the daily recap markdown
#
# All output is tee'd to reports/logs/run-YYYY-MM-DD.log so you can read
# what happened on any past day.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv"
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "[run_daily] missing venv at $VENV - run deploy/setup.sh first" >&2
    exit 1
fi
PY="$VENV/bin/python"
export PYTHONPATH="$ROOT"

TODAY="$(date -u +%Y-%m-%d)"
LOG_DIR="$ROOT/reports/logs"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/run-${TODAY}.log"

echo "[run_daily] $(date -u +%FT%TZ) start" | tee -a "$LOG"

echo "[run_daily] step 1: refresh + daily_runner" | tee -a "$LOG"
"$PY" -m quant_trader.scripts.daily_runner --refresh-data 2>&1 | tee -a "$LOG" || {
    echo "[run_daily] daily_runner FAILED, continuing to positions_check" | tee -a "$LOG"
}

echo "[run_daily] step 2: positions_check" | tee -a "$LOG"
"$PY" -m quant_trader.scripts.positions_check 2>&1 | tee -a "$LOG" || {
    echo "[run_daily] positions_check FAILED" | tee -a "$LOG"
}

echo "[run_daily] step 3: recap" | tee -a "$LOG"
"$PY" -m quant_trader.scripts.recap --date "$TODAY" 2>&1 | tee -a "$LOG" || {
    echo "[run_daily] recap FAILED" | tee -a "$LOG"
}

echo "[run_daily] $(date -u +%FT%TZ) done" | tee -a "$LOG"