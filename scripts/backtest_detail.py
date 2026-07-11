"""Full backtest with daily_loss_limit=0.30 applied."""
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
DAILY_LOSS_LIMIT = 0.30  # leveraged (30%), applied globally

PARAMS = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.0,
    "side": "long_only",
}

strategy = PumpPullbackStrategy(PARAMS)

# Load all symbols
all_syms = store.list_symbols()
symbols = []
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        symbols.append((sym, df))
print(f"Total symbols >= {MIN_BARS} bars: {len(symbols)}")

# Run strategy and collect all trades with dates
all_trades: list[dict] = []
per_symbol: dict[str, list[dict]] = {}

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
            trades.append({
                "entry_ts": str(idx[entry_idx]),
                "exit_ts": str(idx[i]),
                "entry_price": round(entry_p, 8),
                "exit_price": round(exit_p, 8),
                "exit_reason": reason,
                "pnl_pct_lev": round(pnl * 100, 2),
                "bars_held": held,
                "day": str(idx[entry_idx])[:10],
            })
            all_trades.append(pnl)
    if trades:
        per_symbol[sym] = trades

# Apply daily_loss_limit: simulate global risk gate
# Sort all trades by entry_ts globally
all_trades_sorted = []
for sym, trades in per_symbol.items():
    for t in trades:
        all_trades_sorted.append((sym, t))
all_trades_sorted.sort(key=lambda x: x[1]["entry_ts"])

daily_realized: dict[str, float] = defaultdict(float)
daily_wins: dict[str, int] = defaultdict(int)
daily_trades: dict[str, int] = defaultdict(int)
passed = 0
blocked = 0

for sym, t in all_trades_sorted:
    day = t["day"]
    # Check if daily loss limit has been hit
    if daily_realized[day] <= -DAILY_LOSS_LIMIT:
        blocked += 1
        continue
    daily_realized[day] += t["pnl_pct_lev"] / 100
    daily_trades[day] += 1
    if t["pnl_pct_lev"] > 0:
        daily_wins[day] += 1
    passed += 1

print(f"\n=== 风控结果 ===")
print(f"  通过: {passed} | 阻挡: {blocked} | 阻挡率: {blocked/(passed+blocked)*100:.1f}%")

# Per-coin summary (only passed trades)
per_symbol_passed: dict[str, list[dict]] = defaultdict(list)
sym_trades_passed: dict[str, list[float]] = defaultdict(list)
for sym, t in all_trades_sorted:
    day = t["day"]
    # Check the same condition
    fake_daily = sum(x.get("pnl_pct_lev", 0)/100 for x in per_symbol_passed[sym] if x["day"] == day)
    if fake_daily - abs(t["pnl_pct_lev"])/100 <= -DAILY_LOSS_LIMIT and fake_daily <= -DAILY_LOSS_LIMIT:
        continue
    per_symbol_passed[sym].append(t)
    sym_trades_passed[sym].append(t["pnl_pct_lev"])

print(f"\n{'='*130}")
print(f"{'币种':<28} {'开仓数':>6} {'盈利':>5} {'亏损':>5} {'胜率%':>6} {'总收益%':>10} {'均收益%':>9} {'Max收益%':>9} {'Min收益%':>9}")
print(f"{'='*130}")

sym_sorted = sorted(sym_trades_passed.keys(), key=lambda s: sum(sym_trades_passed[s]), reverse=True)
grand_total = 0.0
grand_wins = 0
grand_total_trades = 0
for sym in sym_sorted:
    pnls = sym_trades_passed[sym]
    wins = sum(1 for v in pnls if v > 0)
    n = len(pnls)
    total = sum(pnls)
    avg = total / n if n else 0
    max_v = max(pnls)
    min_v = min(pnls)
    wr = wins / n * 100
    grand_total += total
    grand_wins += wins
    grand_total_trades += n
    sym_short = sym.split("/")[0].split(":")[0]
    print(f"{sym_short:<28} {n:>6} {wins:>5} {n-wins:>5} {wr:>6.1f} {total:>10.2f} {avg:>9.2f} {max_v:>9.2f} {min_v:>9.2f}")

grand_wr = grand_wins / grand_total_trades * 100 if grand_total_trades else 0
print(f"{'='*130}")
print(f"{'总计':<28} {grand_total_trades:>6} {grand_wins:>5} {grand_total_trades-grand_wins:>5} {grand_wr:>6.1f} {grand_total:>10.2f}")
print()

# Overall stats
pnls_a = np.array([t["pnl_pct_lev"] for t in all_trades if t["pnl_pct_lev"] is not None])
total_ret = float(pnls_a.sum())
wr = float((pnls_a > 0).mean()) * 100
sharpe = float(pnls_a.mean() / pnls_a.std()) * np.sqrt(96) if pnls_a.std() > 0 else 0.0
cum = np.cumsum(pnls_a)
peak = np.maximum.accumulate(cum)
max_dd = float((cum - peak).min())
avg_win = float(pnls_a[pnls_a > 0].mean()) if (pnls_a > 0).any() else 0.0
avg_loss = float(pnls_a[pnls_a < 0].mean()) if (pnls_a < 0).any() else 0.0
profit_factor = abs(pnls_a[pnls_a > 0].sum() / pnls_a[pnls_a < 0].sum()) if (pnls_a < 0).sum() != 0 else float('inf')

print(f"{'='*50} 综合统计 (含风控) {'='*50}")
print(f"  总交易数:     {len(pnls_a)}")
print(f"  总收益:       {total_ret:.2f}%")
print(f"  胜率:         {wr:.1f}%")
print(f"  平均盈利:     {avg_win:.2f}%")
print(f"  平均亏损:     {avg_loss:.2f}%")
print(f"  盈亏比:       {abs(avg_win/avg_loss):.2f}")
print(f"  Profit Factor: {profit_factor:.2f}")
print(f"  Sharpe Ratio:  {sharpe:.3f}")
print(f"  最大回撤:     {max_dd:.2f}%")