"""Quick backtest: SL -8% / TP +20% (full close on each)."""
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

BASE = {
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


def backtest(sl_pct, tp_pct, daily_loss_limit, hold_bars=24):
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
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
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
    "v0.4.0 SL=-10% TP=+30% daily=0.50": (-0.10, 0.30, 0.50),
    # 用户的方案
    "用户方案 SL=-8% TP=+20% daily=0.50":   (-0.08, 0.20, 0.50),
    # 衍生
    "变体 SL=-8% TP=+20% daily=0.30":        (-0.08, 0.20, 0.30),
    "变体 SL=-8% TP=+20% daily=0.75":        (-0.08, 0.20, 0.75),
    "变体 SL=-5% TP=+20% daily=0.50":        (-0.05, 0.20, 0.50),
    "变体 SL=-12% TP=+20% daily=0.50":       (-0.12, 0.20, 0.50),
}

for name, (sl, tp, dl) in VARIANTS.items():
    raw, passed, blocked = backtest(sl, tp, dl)
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
    print(f"  通过: {len(passed)} | 阻挡: {blocked} ({blocked/(blocked+len(passed))*100:.1f}%)")
    print(f"  胜率: {win_rate:.1f}% | 盈亏比: {pl_ratio:.2f} | PF: {pf:.2f}")
    print(f"  总收益: {total_ret:+.1f}% | Sharpe: {sharpe:.3f} | 最大回撤: {max_dd:.1f}%")
    print(f"  退出: {reason_str}")
    print()
