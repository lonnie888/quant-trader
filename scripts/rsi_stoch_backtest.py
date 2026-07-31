"""Compare: baseline vs RSI filter (oversold bounce entry)."""
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


def compute_rsi(close, period=14):
    """RSI (Relative Strength Index)"""
    n = len(close)
    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_gain = pd.Series(gain).rolling(period).mean().values
    avg_loss = pd.Series(loss).rolling(period).mean().values
    rs = avg_gain / np.where(avg_loss == 0, 1e-9, avg_loss)
    rsi = 100 - 100 / (1 + rs)
    return rsi


def compute_stoch_k(close, high, low, period=14, smooth=3):
    """Stochastic %K oscillator."""
    n = len(close)
    k = np.zeros(n)
    for i in range(period - 1, n):
        hh = high[i - period + 1: i + 1].max()
        ll = low[i - period + 1: i + 1].min()
        if hh == ll:
            k[i] = 50
        else:
            k[i] = 100 * (close[i] - ll) / (hh - ll)
    return k


def backtest(sl_pct, tp_pct, daily_loss_limit, rsi_low=None, rsi_high=None,
             stoch_low=None, stoch_high=None, hold_bars=24):
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
        # Precompute indicators
        rsi = compute_rsi(close) if (rsi_low is not None or rsi_high is not None) else None
        stoch = compute_stoch_k(close, high, low) if (stoch_low is not None or stoch_high is not None) else None
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
                        continue
                    # RSI filter
                    if rsi is not None and i < len(rsi):
                        if rsi_low is not None and rsi[i] > rsi_low:
                            continue
                        if rsi_high is not None and rsi[i] < rsi_high:
                            continue
                    # Stochastic filter
                    if stoch is not None and i < len(stoch):
                        if stoch_low is not None and stoch[i] > stoch_low:
                            continue
                        if stoch_high is not None and stoch[i] < stoch_high:
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
    "baseline SL=-12% TP=+20%":              (-0.12, 0.20, 0.50, None, None, None, None),
    # RSI oversold: only enter when RSI < 35 (oversold, expect bounce)
    "RSI<35 (超卖入场)":                      (-0.12, 0.20, 0.50, 35, None, None, None),
    "RSI<40":                                  (-0.12, 0.20, 0.50, 40, None, None, None),
    "RSI<45":                                  (-0.12, 0.20, 0.50, 45, None, None, None),
    # RSI not overbought: only enter when RSI < 70 (not overbought yet)
    "RSI<70 (未超买)":                          (-0.12, 0.20, 0.50, None, 70, None, None),
    "RSI<65":                                  (-0.12, 0.20, 0.50, None, 65, None, None),
    "RSI<60":                                  (-0.12, 0.20, 0.50, None, 60, None, None),
    # Stochastic: only enter when %K is in the lower zone (oversold)
    "Stoch<20 (深度超卖)":                     (-0.12, 0.20, 0.50, None, None, 20, None),
    "Stoch<30":                                (-0.12, 0.20, 0.50, None, None, 30, None),
    "Stoch<40":                                (-0.12, 0.20, 0.50, None, None, 40, None),
    # Combined
    "RSI<40 + Stoch<30":                       (-0.12, 0.20, 0.50, 40, None, 30, None),
    "RSI<45 + Stoch<40":                       (-0.12, 0.20, 0.50, 45, None, 40, None),
}

for name, (sl, tp, dl, rsi_lo, rsi_hi, stoch_lo, stoch_hi) in VARIANTS.items():
    raw, passed, blocked = backtest(sl, tp, dl, rsi_lo, rsi_hi, stoch_lo, stoch_hi)
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
