"""Full backtest with v0.3.7 params (trigger_pct=0.003)."""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from quant_trader.config import load_settings
from quant_trader.data.storage.parquet_store import ParquetStore
from quant_trader.strategy.library.pump_pullback import PumpPullbackStrategy

settings = load_settings()
store = ParquetStore(settings.data.storage_dir)

LEVERAGE = 3.0
FEE_RATE = 0.0004
SLIPPAGE = 0.0005
MIN_BARS = 500
DAILY_LOSS_LIMIT = 0.30

PARAMS = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.003, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.0,
    "side": "long_only",
}

strategy = PumpPullbackStrategy(PARAMS)

all_syms = store.list_symbols()
symbols = []
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        symbols.append((sym, df))
print(f"加载 {len(symbols)} 个币种 >= {MIN_BARS} 根 K 线")

# --- 1. 生成原始信号 ---
per_symbol: dict[str, list[dict]] = {}
raw_trades_all: list[tuple[str, dict]] = []

for sym, df in symbols:
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    idx = df.index
    try:
        sigs = strategy.generate_signals(df)
    except Exception:
        continue
    if sigs.empty or sigs.sum() == 0:
        continue
    s = sigs.values
    n = len(s)
    trades = []
    in_pos = False
    entry_p = 0.0
    entry_idx = 0
    held = 0
    sl_p = 0.0
    for i in range(n):
        if not in_pos:
            if s[i] == 1:
                in_pos = True
                entry_p = close[i] * (1 + SLIPPAGE)
                entry_idx = i
                held = 0
                sl_p = entry_p * (1 - float(PARAMS["stop_loss_pct"]))
        else:
            held += 1
            if low[i] <= sl_p:
                exit_p = sl_p * (1 - SLIPPAGE)
                reason = "sl"
                in_pos = False
            elif held >= int(PARAMS["hold_bars"]) or s[i] == 0:
                exit_p = close[i] * (1 - SLIPPAGE)
                reason = "time" if held >= int(PARAMS["hold_bars"]) else "signal"
                in_pos = False
            else:
                continue
            pnl = (exit_p - entry_p) / entry_p * LEVERAGE - FEE_RATE * 2
            t = {
                "entry_ts": str(idx[entry_idx]),
                "exit_ts": str(idx[i]),
                "entry_price": round(entry_p, 8),
                "exit_price": round(exit_p, 8),
                "exit_reason": reason,
                "pnl_pct_lev": round(pnl * 100, 2),
                "bars_held": held,
                "day": str(idx[entry_idx])[:10],
            }
            trades.append(t)
            raw_trades_all.append((sym, t))
    if trades:
        per_symbol[sym] = trades

print(f"原始信号交易: {len(raw_trades_all)} 笔")

# --- 2. 应用 daily_loss_limit 风控 ---
raw_trades_all.sort(key=lambda x: x[1]["entry_ts"])

daily_realized: dict[str, float] = defaultdict(float)
passed = []
blocked = 0

for sym, t in raw_trades_all:
    day = t["day"]
    if daily_realized[day] <= -DAILY_LOSS_LIMIT:
        blocked += 1
        continue
    daily_realized[day] += t["pnl_pct_lev"] / 100
    passed.append((sym, t))

print(f"风控后: 通过 {len(passed)} | 阻挡 {blocked} | 阻挡率 {blocked/(blocked+len(passed))*100:.1f}%")

# --- 3. 汇总统计 ---
per_sym_passed: dict[str, list[float]] = defaultdict(list)
for sym, t in passed:
    per_sym_passed[sym].append(t["pnl_pct_lev"])

grand_total = 0.0
grand_wins = 0
grand_n = 0

print(f"\n{'='*130}")
print(f"{'币种':<25} {'开仓数':>6} {'盈利':>5} {'亏损':>5} {'胜率%':>6} {'总收益%':>10} {'均收益%':>9}")
print(f"{'='*130}")

sorted_syms = sorted(per_sym_passed.keys(), key=lambda s: sum(per_sym_passed[s]), reverse=True)
for sym in sorted_syms:
    pnls = per_sym_passed[sym]
    wins = sum(1 for v in pnls if v > 0)
    n = len(pnls)
    total = sum(pnls)
    avg = total / n if n else 0
    wr = wins / n * 100
    grand_total += total
    grand_wins += wins
    grand_n += n
    short = sym.split("/")[0].split(":")[0]
    print(f"{short:<25} {n:>6} {wins:>5} {n-wins:>5} {wr:>6.1f} {total:>10.2f} {avg:>9.2f}")

grand_wr = grand_wins / grand_n * 100 if grand_n else 0
print(f"{'='*130}")
print(f"{'总计':<25} {grand_n:>6} {grand_wins:>5} {grand_n-grand_wins:>5} {grand_wr:>6.1f} {grand_total:>10.2f}")
print()

# --- 4. 综合指标 ---
pnls_a = np.array([t["pnl_pct_lev"] for _, t in passed])
total_ret = float(pnls_a.sum())
wr = float((pnls_a > 0).mean()) * 100
sharpe = float(pnls_a.mean() / pnls_a.std()) * np.sqrt(96) if pnls_a.std() > 0 else 0.0
cum = np.cumsum(pnls_a)
peak = np.maximum.accumulate(cum)
max_dd = float((cum - peak).min())
avg_win = float(pnls_a[pnls_a > 0].mean()) if (pnls_a > 0).any() else 0.0
avg_loss = float(pnls_a[pnls_a < 0].mean()) if (pnls_a < 0).any() else 0.0
profit_factor = abs(pnls_a[pnls_a > 0].sum() / pnls_a[pnls_a < 0].sum()) if (pnls_a < 0).sum() != 0 else float('inf')

print(f"{'='*50} v0.3.7 综合统计 (含风控) {'='*50}")
print(f"  trigger_pct:               0.003")
print(f"  daily_loss_limit:          0.30")
print(f"")
print(f"  总交易数:                  {len(pnls_a)}")
print(f"  总收益:                    {total_ret:.2f}%")
print(f"  胜率:                      {wr:.1f}%")
print(f"  平均盈利:                  {avg_win:.2f}%")
print(f"  平均亏损:                  {avg_loss:.2f}%")
print(f"  盈亏比:                    {abs(avg_win/avg_loss):.2f}")
print(f"  Profit Factor:             {profit_factor:.2f}")
print(f"  Sharpe Ratio:              {sharpe:.3f}")
print(f"  最大回撤:                  {max_dd:.2f}%")

# --- 5. Top/Bottom trades ---
all_p = [(sym, t) for sym, t in passed]
all_p.sort(key=lambda x: x[1]["pnl_pct_lev"], reverse=True)

print(f"\n{'='*130}")
print(f"Top 15 最佳交易")
print(f"{'='*130}")
print(f"{'币种':<25} {'入场价':>12} {'退出价':>12} {'收益%':>8} {'原因':>8} {'持仓K线':>8}")
print(f"{'-'*130}")
for sym, t in all_p[:15]:
    s = sym.split("/")[0].split(":")[0]
    print(f"{s:<25} {t['entry_price']:>12.6f} {t['exit_price']:>12.6f} {t['pnl_pct_lev']:>8.2f} {t['exit_reason']:>8} {t['bars_held']:>8}")

print(f"\n{'='*130}")
print(f"Bottom 15 最差交易")
print(f"{'='*130}")
print(f"{'币种':<25} {'入场价':>12} {'退出价':>12} {'收益%':>8} {'原因':>8} {'持仓K线':>8}")
print(f"{'-'*130}")
for sym, t in all_p[-15:]:
    s = sym.split("/")[0].split(":")[0]
    print(f"{s:<25} {t['entry_price']:>12.6f} {t['exit_price']:>12.6f} {t['pnl_pct_lev']:>8.2f} {t['exit_reason']:>8} {t['bars_held']:>8}")

# Save
out = {
    "params": PARAMS,
    "stats": {
        "total_trades": len(pnls_a),
        "total_return%": round(total_ret, 2),
        "win_rate%": round(wr, 1),
        "avg_win%": round(avg_win, 2),
        "avg_loss%": round(avg_loss, 2),
        "sharpe": round(sharpe, 3),
        "max_dd%": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2),
        "blocked": blocked,
    },
    "per_symbol": {s: [t for sym_, t in passed if sym_ == s] for s in sorted_syms},
}
Path("reports/paper/backtest_v037.json").write_text(json.dumps(out, indent=2, ensure_ascii=False))
print(f"\n✅ 详细数据保存到 reports/paper/backtest_v037.json")