#!/bin/bash
# 模拟实盘回测 - 自动串行跑所有批次 (支持自定义保证金比例)
cd /vol1/1000/quant_trader
EQUITY=10.0
MAX_CONCURRENT=${1:-3}
MARGIN=${2:-0.20}
LABEL="mc${MAX_CONCURRENT}_mp${MARGIN}"
echo "max_concurrent=$MAX_CONCURRENT margin_pct=$MARGIN"
echo "" > "reports/paper/sim_${LABEL}_all.txt"

for i in $(seq 0 35); do
  echo "=== 批次 $i ==="
  OUTPUT="reports/paper/sim_${LABEL}_b${i}.json"
  .venv/bin/python3 scripts/sim_real_bt.py --initial $EQUITY --max-concurrent $MAX_CONCURRENT --margin-pct $MARGIN --batch $i --resume-from $EQUITY --output $OUTPUT 2>&1 | tail -10
  if [ -f "$OUTPUT" ]; then
    NEW_EQUITY=$(python3 -c "import json; print(json.load(open('$OUTPUT'))['final_equity'])" 2>/dev/null)
    if [ -n "$NEW_EQUITY" ]; then
      EQUITY=$NEW_EQUITY
      echo "批次$i 权益=$EQUITY" >> "reports/paper/sim_${LABEL}_all.txt"
    fi
  fi
  echo ""
done

echo "=== 全部完成 ==="
echo "最终权益: $EQUITY"
echo "总收益: $(python3 -c "print(f'{($EQUITY/10.0-1)*100:.2f}%')")"