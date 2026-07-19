"""Compare bars_since variants: 1/2/3/5/8 (current=2)."""
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

# Note: pump_pullback doesn't have explicit bars_since — the protection is done
# at runtime by skipping signals with bars_since > threshold.
# We simulate this here by checking the signal against pump-bar distance.
# For backtest, we'll just compare hold_bars/cooldown which is the strategy-internal
# equivalent of "how long after signal can we enter".

BASE = {
    "pump_window": 12,
    "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.30,
    "side": "long_only",
}

# Load data once
all_syms = store.list_symbols()
symbols = []
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        symbols.append((sym, df))
print(f"加载 {len(symbols)} 个币种\n")

def find_pump_idx(close, window, threshold):
    """Find indices where the close just crossed UP through threshold in last window bars."""
    n = len(close)
    pump_idx = []
    for i in range(window, n):
        past = close[i - window]
        if past <= 0: continue
        ret = (close[i] - past) / past
        if ret >= threshold:
            pump_idx.append(i)
    return pump_idx

def backtest_max_bars(max_bars_after_pump):
    """Simulate: only enter within N bars after the pump-bar."""
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
        # Find pump indices for this symbol
        pump_idx = find_pump_idx(close, BASE["pump_window"], BASE["pump_threshold"])
        if not pump_idx:
            continue
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    # Check if signal is within N bars of any pump
                    valid = False
                    for pi in pump_idx:
                        if 0 <= i - pi <= max_bars_after_pump:
                            valid = True
                            break
                    if not valid:
                        continue
                    in_pos = True
                    entry_p = close[i] * (1 + SLIPPAGE)
                    entry_idx = i
                    held = 0
                    sl_p = entry_p * (1 - float(BASE["stop_loss_pct"]))
                    tp_p = entry_p * (1 + float(BASE["take_profit_pct"]))
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
                elif held >= int(BASE["hold_bars"]) or s[i] == 0:
                    exit_p = close[i] * (1 - SLIPPAGE)
                    reason = "time" if held >= int(BASE["hold_bars"]) else "signal"
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

VARIANTS = {
    "v0.4.0 (no filter)": 999,
    "max_bars=1 (strict)":  1,
    "max_bars=2 (current)": 2,
    "max_bars=3":          3,
    "max_bars=5":          5,
    "max_bars=8":          8,
}

print(f"{'变体':<25} {'原始':>6} {'通过':>6} {'阻挡率':>7} {'总收益%':>10} {'胜率%':>7} {'盈亏比':>7} {'PF':>6} {'Sharpe':>7}")
print("-" * 100)
for name, mb in VARIANTS.items():
    raw, passed, blocked = backtest_max_bars(mb)
    pnls = [t["pnl_pct_lev"] for t in passed]
    if not pnls:
        print(f"{name:<25}     0      0    0.0%        0.0%    0.0%    0.00  0.00    0.00")
        continue
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1
    pl_ratio = avg_win / avg_loss if avg_loss else 0
    pf = sum(wins) / abs(sum(losses)) if losses else float('inf')
    total_ret = sum(pnls)
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    print(f"{name:<25} {len(raw):>6} {len(passed):>6} {blocked/(blocked+len(passed))*100:>6.1f}% {total_ret:>9.1f}% {win_rate:>6.1f}% {pl_ratio:>6.2f} {pf:>5.2f} {sharpe:>6.3f}")
