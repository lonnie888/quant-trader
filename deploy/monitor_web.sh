#!/bin/bash
# Monitor web dashboard, restart if down
WEB_CMD="/vol1/1000/quant_trader/.venv/bin/python3 web/app.py"
LOG_DIR="/vol1/1000/quant_trader/reports/logs"

if ! curl -s -o /dev/null -w "" http://localhost:5050/ 2>/dev/null; then
    cd /vol1/1000/quant_trader
    nohup $WEB_CMD >> "$LOG_DIR/web.log" 2>&1 &
    echo "$(date): web restarted PID $!" >> "$LOG_DIR/autostart.log"
fi