#!/bin/bash
# Auto-start quant_trader daemon if not running (web is handled by systemd)
DAEMON_CMD="/vol1/1000/quant_trader/.venv/bin/python3 -m quant_trader.scripts.daemon"
LOG_DIR="/vol1/1000/quant_trader/reports/logs"
mkdir -p "$LOG_DIR"

if ! pgrep -f "quant_trader.scripts.daemon" > /dev/null; then
    cd /vol1/1000/quant_trader
    nohup $DAEMON_CMD >> "$LOG_DIR/daemon.log" 2>&1 &
    echo "$(date): daemon started PID $!" >> "$LOG_DIR/autostart.log"
fi