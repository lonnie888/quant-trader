"""Compare: baseline vs Bollinger Bands filter."""
from __future__ import annotations

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

BASE = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.12, "take_profit_pct": 0.20,
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


def compute_bollinger(close, period=20, stddev=2.0):
    """Compute Bollinger Bands (upper, middle, lower)."""
    n = len(close)
    mid = pd.Series(close).rolling(period).mean().values
    std = pd.Series(close).rolling(period).std().values
    upper = mid + stddev * std
    lower = mid - stddev * std
    # %B = (close - lower) / (upper - lower)
    width = upper - lower
    pct_b = np.where(width > 0, (close - lower) / width, 0.5)
    return upper, mid, lower, pct_b


def backtest(sl_pct, tp_pct, daily_loss_limit,
             bb_filter=None,  # dict: 'low':0.2, 'high':0.8 (entry only when %B in range)
             hold_bars=24):
    strategy = PumpPullbackStrategy(BASE)
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
        pump_idx = find_pump_idx(close, BASE["pump_window"], BASE["pump_threshold"])
        if not pump_idx:
            continue
        upper, mid, lower, pct_b = compute_bollinger(close) if bb_filter else (None, None, None, None)
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
                        continue
                    # Bollinger filter
                    if bb_filter is not None and i < len(pct_b):
                        if bb_filter.get('low') is not None and pct_b[i] < bb_filter['low']:
                            continue
                        if bb_filter.get('high') is not None and pct_b[i] > bb_filter['high']:
                            continue
                    in_pos = True
                    entry_p = close[i] * (1 + SLIPPAGE)
                    entry_idx = i
                    held = 0
                    sl_p = entry_p * (1 + sl_pct)
                    tp_p = entry_p * (1 + tp_pct)
            else:
                held += 1
                if low[i] <= sl_p:
                    exit_p = sl_p * (1 - SLIPPAGE)
                    reason = "sl"
                    in_pos = False
                elif high[i] >= tp_p:
                    exit_p = tp_p * (1 - SLIPPAGE)
                    reason = "tp"
                    in_pos = False
                elif held >= hold_bars or s[i] == 0:
                    exit_p = close[i] * (1 - SLIPPAGE)
                    reason = "time" if held >= hold_bars else "signal"
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
        if daily[d] <= -daily_loss_limit:
            blocked += 1
            continue
        daily[d] += t["pnl_pct_lev"] / 100
        passed.append(t)
    return raw, passed, blocked


VARIANTS = {
    # baseline
    "baseline SL=-12% TP=+20%":                    (-0.12, 0.20, 0.50, None),
    # BB %B: only enter in lower half of bands
    "%B<0.2 (极度接近下轨)":                        (-0.12, 0.20, 0.50, {'low': 0.2, 'high': None}),
    "%B<0.3":                                       (-0.12, 0.20, 0.50, {'low': 0.3, 'high': None}),
    "%B<0.4":                                       (-0.12, 0.20, 0.50, {'low': 0.4, 'high': None}),
    "%B<0.5 (中轨以下)":                            (-0.12, 0.20, 0.50, {'low': 0.5, 'high': None}),
    # BB %B: not in upper band (not overbought)
    "%B<0.7":                                       (-0.12, 0.20, 0.50, {'low': None, 'high': 0.7}),
    "%B<0.8":                                       (-0.12, 0.20, 0.50, {'low': None, 'high': 0.8}),
    "%B<0.9":                                       (-0.12, 0.20, 0.50, {'low': None, 'high': 0.9}),
    # BB %B: middle range
    "0.1<%B<0.5 (下半场)":                          (-0.12, 0.20, 0.50, {'low': 0.1, 'high': 0.5}),
    "0.2<%B<0.6":                                   (-0.12, 0.20, 0.50, {'low': 0.2, 'high': 0.6}),
    "0.3<%B<0.7":                                   (-0.12, 0.20, 0.50, {'low': 0.3, 'high': 0.7}),
}

for name, (sl, tp, dl, bb_f) in VARIANTS.items():
    raw, passed, blocked = backtest(sl, tp, dl, bb_f)
    pnls = [t["pnl_pct_lev"] for t in passed]
    if not pnls:
        print(f"{name}: no trades\n")
        continue
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1
    pl_ratio = avg_win / avg_loss if avg_loss else 0
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    total_ret = sum(pnls)
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    max_dd = np.min(cum - peak) if len(cum) > 0 else 0
    reasons = defaultdict(int)
    for t in passed:
        reasons[t["exit_reason"]] += 1
    reason_str = " | ".join(f"{k}:{v}" for k, v in sorted(reasons.items()))
    print(f"{name}")
    print(f"  通过: {len(passed):4d} | 阻挡: {blocked:4d} ({blocked/(blocked+len(passed))*100:.1f}%)")
    print(f"  胜率: {win_rate:.1f}% | 盈亏比: {pl_ratio:.2f} | PF: {pf:.2f}")
    print(f"  总收益: {total_ret:+8.1f}% | Sharpe: {sharpe:.3f} | 最大回撤: {max_dd:+.1f}%")
    print(f"  退出: {reason_str}")
    print()
