#!/bin/bash
# 安全停止 quant_trader daemon (避免命令行自匹配误杀)
pids=$(pgrep -f 'quant_trader[.]scripts[.]daemon')
if [ -n "$pids" ]; then
  echo "stopping: $pids"
  for p in $pids; do kill -9 "$p" 2>/dev/null; done
  sleep 2
fi
pgrep -f 'quant_trader[.]scripts[.]daemon' && echo "WARN: still running" || echo "daemon stopped"
