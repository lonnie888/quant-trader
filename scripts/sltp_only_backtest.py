"""Backtest ONLY with fixed SL/TP exits (no time-based exit, no signal exit)."""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

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
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.30,
    "side": "long_only",
}

all_syms = store.list_symbols()
symbols = []
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        symbols.append((sym, df))
print(f"加载 {len(symbols)} 个币种\n")


def find_pump_idx(close, window, threshold):
    n = len(close)
    out = []
    for i in range(window, n):
        past = close[i - window]
        if past <= 0: continue
        if (close[i] - past) / past >= threshold:
            out.append(i)
    return out


def backtest():
    strategy = PumpPullbackStrategy(PARAMS)
    raw = []
    for sym, df in symbols:
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index
        n = len(s)
        pump_idx = find_pump_idx(close, PARAMS["pump_window"], PARAMS["pump_threshold"])
        if not pump_idx:
            continue
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    # bars_since=1 filter
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
                        continue
                    in_pos = True
                    entry_p = close[i] * (1 + SLIPPAGE)
                    entry_idx = i
                    sl_p = entry_p * (1 - float(PARAMS["stop_loss_pct"]))
                    tp_p = entry_p * (1 + float(PARAMS["take_profit_pct"]))
            else:
                # SL/TP only — no time exit, no signal exit
                if low[i] <= sl_p:
                    exit_p = sl_p * (1 - SLIPPAGE)
                    reason = "sl"
                    in_pos = False
                elif high[i] >= tp_p:
                    exit_p = tp_p * (1 - SLIPPAGE)
                    reason = "tp"
                    in_pos = False
                else:
                    continue
                pnl = (exit_p - entry_p) / entry_p * LEVERAGE - FEE_RATE * 2
                raw.append((sym, {
                    "entry_ts": str(idx[entry_idx]),
                    "exit_ts": str(idx[i]),
                    "pnl_pct_lev": pnl * 100,
                    "exit_reason": reason,
                    "day": str(idx[entry_idx])[:10],
                }))
    raw.sort(key=lambda x: x[1]["entry_ts"])
    daily = defaultdict(float)
    passed = []
    blocked = 0
    for sym, t in raw:
        d = t["day"]
        if daily[d] <= -DAILY_LOSS_LIMIT:
            blocked += 1
            continue
        daily[d] += t["pnl_pct_lev"] / 100
        passed.append(t)
    return raw, passed, blocked


raw, passed, blocked = backtest()
pnls = [t["pnl_pct_lev"] for t in passed]
wins = [p for p in pnls if p > 0]
losses = [p for p in pnls if p <= 0]
tp_count = sum(1 for t in passed if t["exit_reason"] == "tp")
sl_count = sum(1 for t in passed if t["exit_reason"] == "sl")
tp_avg = np.mean([t["pnl_pct_lev"] for t in passed if t["exit_reason"] == "tp"]) if tp_count else 0
sl_avg = np.mean([t["pnl_pct_lev"] for t in passed if t["exit_reason"] == "sl"]) if sl_count else 0

print("=" * 80)
print("纯 SL/TP 触发回测（无 time / signal 退出）")
print("=" * 80)
print(f"  原始信号:                {len(raw)} 笔")
print(f"  风控后通过:              {len(passed)} 笔 (阻挡 {blocked}, 阻挡率 {blocked/(len(raw)+blocked)*100:.1f}%)")
print()
print(f"  TP 触发:                 {tp_count} 笔 (平均 {tp_avg:+.2f}%)")
print(f"  SL 触发:                 {sl_count} 笔 (平均 {sl_avg:+.2f}%)")
print(f"  TP/SL 比:                {tp_count/sl_count:.2f}" if sl_count else "")
print()
print(f"  胜率:                    {len(wins)/len(pnls)*100:.1f}%")
print(f"  总收益:                  {sum(pnls):+.2f}%")
print(f"  平均盈利:                {np.mean(wins):+.2f}%" if wins else "")
print(f"  平均亏损:                {np.mean(losses):+.2f}%" if losses else "")
print(f"  盈亏比:                  {abs(np.mean(wins)/np.mean(losses)):.2f}" if wins and losses else "")
print(f"  Profit Factor:           {sum(wins)/abs(sum(losses)):.2f}" if losses else "")
print(f"  Sharpe:                  {(np.mean(pnls)/np.std(pnls))*np.sqrt(len(pnls)):.3f}" if len(pnls) > 1 else "")
print()
# 期望值
ev_per_trade = (tp_count * tp_avg + sl_count * sl_avg) / (tp_count + sl_count)
print(f"  期望收益/笔:             {ev_per_trade:+.2f}%")
print()
# 按退出原因分桶
print("=" * 80)
print("Top 10 最佳 TP 交易")
print("=" * 80)
tp_trades = sorted([t for t in passed if t["exit_reason"] == "tp"], key=lambda x: -x["pnl_pct_lev"])[:10]
for t in tp_trades:
    print(f"  {t['day']}  {t['pnl_pct_lev']:+7.2f}%  {t['entry_ts']} -> {t['exit_ts']}")
