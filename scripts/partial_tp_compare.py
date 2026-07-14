"""Partial TP + trailing stop backtest.

- 30% gain: close 90% of position, leave 10% runner
- runner: trailing stop from peak. If peak > entry * 1.30,
  exit when price drops more than trail_pct from peak.
- Stop loss: 10% from entry
"""
from __future__ import annotations

import sys, time
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
FEE = 0.0004
SLIP = 0.0005
MIN_BARS = 1000
DAILY_LOSS_LIMIT = 0.30

BASE = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "side": "long_only",
}

# Pre-load
t0 = time.time()
all_syms = store.list_symbols()
data = {}
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        data[sym] = df
print(f"Loaded {len(data)} symbols in {time.time()-t0:.0f}s")

# Variants: (TP trigger %, partial close %, trail drop %)
# Once price hits entry * (1 + TP_trigger), close partial_pct of position.
# Remaining (1 - partial_pct) rides with trailing stop.
# If remaining peak > entry * (1 + TP_trigger) and price drops by trail_pct from peak, exit.
# Hard SL on remaining: -10% from entry.
VARIANTS = [
    ("baseline (TP=0)", 0.0, 1.0, 0.0),
    ("TP30% full", 0.30, 1.0, 0.0),
    ("TP30% 平90% trail10%", 0.30, 0.90, 0.10),
    ("TP30% 平90% trail15%", 0.30, 0.90, 0.15),
    ("TP30% 平90% trail20%", 0.30, 0.90, 0.20),
    ("TP50% 平90% trail10%", 0.50, 0.90, 0.10),
    ("TP50% 平90% trail15%", 0.50, 0.90, 0.15),
    ("TP30% 平70% trail10%", 0.30, 0.70, 0.10),
]

def run(tp_trig, partial, trail_pct):
    params = dict(BASE, take_profit_pct=0.0)  # TP handled manually below
    strategy = PumpPullbackStrategy(params)
    all_raw = []
    for sym, df in data.items():
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        n = len(s)
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index
        in_pos = False
        ep = 0.0
        ei = 0
        sl_p = 0.0
        # partial tp state
        partial_done = False  # whether 90% has been closed
        remaining_qty = 1.0  # start with 100%
        peak = 0.0
        # simulated split weights
        w_sl = remaining_qty
        w_tp = 0.0
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    in_pos = True
                    ep = close[i] * (1 + SLIP)
                    ei = i
                    sl_p = ep * (1 - BASE["stop_loss_pct"])
                    partial_done = False
                    peak = ep
                    remaining_qty = 1.0
            else:
                # update peak from high
                peak = max(peak, high[i])
                # 1. SL check (bar-internal)
                if low[i] <= sl_p:
                    pnl = (sl_p * (1 - SLIP) - ep) / ep * LEVERAGE * remaining_qty - FEE * 2 * remaining_qty
                    all_raw.append({"pnl": pnl * 100, "reason": "sl", "day": str(idx[ei])[:10]})
                    in_pos = False
                    continue
                # 2. partial TP (only once)
                if not partial_done and tp_trig > 0 and high[i] >= ep * (1 + tp_trig):
                    # close partial_pct at TP price
                    tp_price = ep * (1 + tp_trig) * (1 - SLIP)
                    pnl_partial = (tp_price - ep) / ep * LEVERAGE * partial - FEE * 2 * partial
                    all_raw.append({"pnl": pnl_partial * 100, "reason": "tp_partial", "day": str(idx[ei])[:10]})
                    remaining_qty = 1.0 - partial
                    partial_done = True
                    # adjust SL for remaining (move to breakeven)
                    sl_p = ep  # new SL = entry
                # 3. trailing stop on remaining
                if partial_done and trail_pct > 0:
                    trail_price = peak * (1 - trail_pct)
                    if low[i] <= trail_price:
                        pnl = (trail_price * (1 - SLIP) - ep) / ep * LEVERAGE * remaining_qty - FEE * 2 * remaining_qty
                        all_raw.append({"pnl": pnl * 100, "reason": "trail", "day": str(idx[ei])[:10]})
                        in_pos = False
                        continue
                # 4. time exit
                held = i - ei
                if held >= BASE["hold_bars"] or s[i] == 0:
                    exit_p = close[i] * (1 - SLIP)
                    pnl = (exit_p - ep) / ep * LEVERAGE * remaining_qty - FEE * 2 * remaining_qty
                    all_raw.append({"pnl": pnl * 100, "reason": "time", "day": str(idx[ei])[:10]})
                    in_pos = False
    # apply daily_loss_limit
    all_raw.sort(key=lambda x: x["day"])
    daily = {}
    passed = []
    blocked = 0
    for t in all_raw:
        if daily.get(t["day"], 0) <= -DAILY_LOSS_LIMIT:
            blocked += 1
            continue
        daily[t["day"]] = daily.get(t["day"], 0) + t["pnl"] / 100
        passed.append(t)
    return passed, blocked, all_raw

def stats(passed):
    if not passed:
        return {}
    pnls = np.array([t["pnl"] for t in passed])
    total = float(pnls.sum())
    wr = float((pnls > 0).mean()) * 100
    aw = float(pnls[pnls > 0].mean()) if (pnls > 0).any() else 0
    al = float(pnls[pnls < 0].mean()) if (pnls < 0).any() else 0
    sh = float(pnls.mean() / pnls.std()) * np.sqrt(96) if pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    md = float((cum - np.maximum.accumulate(cum)).min())
    pf = abs(sum(pnls[pnls > 0]) / sum(pnls[pnls < 0])) if (pnls < 0).sum() != 0 else float("inf")
    return {
        "trades": len(pnls),
        "total": round(total, 2),
        "wr": round(wr, 1),
        "sharpe": round(sh, 3),
        "dd": round(md, 2),
        "pf": round(pf, 2),
        "avg_w": round(aw, 2),
        "avg_l": round(al, 2),
    }

for name, tp_trig, partial, trail in VARIANTS:
    t0 = time.time()
    passed, blocked, _ = run(tp_trig, partial, trail)
    el = time.time() - t0
    s = stats(passed)
    if not s:
        print(f"\n{name}: 0 trades")
        continue
    reasons = {}
    for t in passed:
        r = t["reason"]
        reasons[r] = reasons.get(r, 0) + 1
    print(f"\n{'='*80}")
    print(f"{name}  ({el:.0f}s, blocked={blocked})")
    print(f"{'='*80}")
    print(f"  收益:{s['total']:>8}%  胜率:{s['wr']:>5}%   Sharpe:{s['sharpe']:.3f}  回撤:{s['dd']:>6}%  PF:{s['pf']}")
    print(f"  均盈:{s['avg_w']:>5}%  均亏:{s['avg_l']:>5}%   交易:{s['trades']}  退出:{reasons}")