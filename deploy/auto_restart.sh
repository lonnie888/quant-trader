#!/bin/bash
# Auto-start quant_trader processes if not running
DAEMON_CMD="/vol1/1000/quant_trader/.venv/bin/python3 -m quant_trader.scripts.daemon"
WEB_CMD="/vol1/1000/quant_trader/.venv/bin/python3 web/app.py"
LOG_DIR="/vol1/1000/quant_trader/reports/logs"
mkdir -p "$LOG_DIR"

# Start daemon
if ! pgrep -f "quant_trader.scripts.daemon" > /dev/null; then
    cd /vol1/1000/quant_trader
    nohup $DAEMON_CMD >> "$LOG_DIR/daemon.log" 2>&1 &
    echo "$(date): daemon started PID $!" >> "$LOG_DIR/autostart.log"
fi

# Start web
if ! pgrep -f "web/app.py" > /dev/null; then
    cd /vol1/1000/quant_trader
    nohup $WEB_CMD >> "$LOG_DIR/web.log" 2>&1 &
    echo "$(date): web started PID $!" >> "$LOG_DIR/autostart.log"
fi