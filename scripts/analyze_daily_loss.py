"""Analyze daily PnL distribution to find optimal daily_loss_limit."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import numpy as np

data = json.loads(Path("reports/paper/backtest_detail.json").read_text())
per_sym = data["per_symbol"]

daily_pnl: dict[str, float] = defaultdict(float)
daily_trades: dict[str, int] = defaultdict(int)

for sym, trades in per_sym.items():
    for t in trades:
        day = t["entry_ts"][:10]
        pnl = t["pnl_pct_lev"] / 100
        daily_pnl[day] += pnl
        daily_trades[day] += 1

pnls = np.array(list(daily_pnl.values()))
print(f"总天数: {len(pnls)}")
print(f"日均交易: {np.mean(list(daily_trades.values())):.1f}\n")

# Leveraged distribution
print("=== 日收益分布 (leveraged) ===")
for p in [1, 5, 10, 25, 50, 75, 90, 95, 99]:
    val = np.percentile(pnls, p)
    print(f"  {p:>3}th: {val*100:+6.2f}%")

# Raw distribution (÷3)
print("\n=== 日收益分布 (raw, ÷3) ===")
for p in [1, 5, 10, 25, 50, 75, 90, 95, 99]:
    val = np.percentile(pnls, p) / 3
    print(f"  {p:>3}th: {val*100:+6.2f}%")

print("\n=== 最差5天 (raw) ===")
for val, day in sorted([(v, d) for d, v in daily_pnl.items()], key=lambda x: x[0])[:5]:
    print(f"  {day}: leveraged={val*100:+.2f}%  raw={val/3*100:+.2f}%  trades={daily_trades[day]}")

print("\n=== daily_loss_limit 建议 ===")
print("(按 raw % 算, 对比被阻断的天数比例)")
for limit_raw in [0.03, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30]:
    limit_lev = limit_raw * 3  # 3x leverage for comparison
    blocked = int((pnls <= -limit_lev).sum())
    pct = blocked / len(pnls) * 100
    print(f"  {limit_raw*100:>3.0f}% raw ({limit_lev*100:>3.0f}% lev): {blocked}/{len(pnls)} 天 = {pct:.1f}%")