"""Fast TP comparison: pre-load data, then test each TP."""
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

# Pre-load data once
t0 = time.time()
all_syms = store.list_symbols()
data = {}
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        data[sym] = {
            "close": df["close"].values,
            "high": df["high"].values,
            "low": df["low"].values,
        }
print(f"Loaded {len(data)} symbols in {time.time()-t0:.0f}s")

def run(tp):
    params = dict(BASE, take_profit_pct=tp)
    strategy = PumpPullbackStrategy(params)
    all_raw = []
    for sym, d in data.items():
        df = store.load(sym, "15m")
        if df.empty:
            continue
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        n = len(s)
        # use df arrays directly to match sigs length
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index
        in_pos = False
        ep = 0.0
        ei = 0
        sl_p = 0.0
        tp_p = 0.0
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    in_pos = True
                    ep = close[i] * (1 + SLIP)
                    ei = i
                    sl_p = ep * (1 - BASE["stop_loss_pct"])
                    tp_p = ep * (1 + tp) if tp > 0 else float("inf")
            else:
                if low[i] <= sl_p:
                    pnl = (sl_p * (1 - SLIP) - ep) / ep * LEVERAGE - FEE * 2
                    all_raw.append({"pnl": pnl * 100, "reason": "sl", "day": str(idx[ei])[:10]})
                    in_pos = False
                elif tp > 0 and high[i] >= tp_p:
                    pnl = (tp_p * (1 - SLIP) - ep) / ep * LEVERAGE - FEE * 2
                    all_raw.append({"pnl": pnl * 100, "reason": "tp", "day": str(idx[ei])[:10]})
                    in_pos = False
                else:
                    held = i - ei
                    if held >= BASE["hold_bars"] or s[i] == 0:
                        pnl = (close[i] * (1 - SLIP) - ep) / ep * LEVERAGE - FEE * 2
                        all_raw.append({"pnl": pnl * 100, "reason": "time", "day": str(idx[ei])[:10]})
                        in_pos = False
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

for tp_val in [0.0, 0.3, 0.5, 1.0, 2.0]:
    t0 = time.time()
    passed, blocked, _ = run(tp_val)
    el = time.time() - t0
    pnls = np.array([t["pnl"] for t in passed])
    if len(pnls) == 0:
        print(f"\nTP={tp_val:.0%}: 0 trades")
        continue
    total = float(pnls.sum())
    wr = float((pnls > 0).mean()) * 100
    aw = float(pnls[pnls > 0].mean()) if (pnls > 0).any() else 0
    al = float(pnls[pnls < 0].mean()) if (pnls < 0).any() else 0
    sh = float(pnls.mean() / pnls.std()) * np.sqrt(96) if pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    md = float((cum - np.maximum.accumulate(cum)).min())
    sl = sum(1 for t in passed if t["reason"] == "sl")
    tp = sum(1 for t in passed if t["reason"] == "tp")
    ti = sum(1 for t in passed if t["reason"] == "time")
    pf = abs(sum(pnls[pnls > 0]) / sum(pnls[pnls < 0])) if (pnls < 0).sum() != 0 else float("inf")
    print(f"\nTP={tp_val*100:3.0f}%  trades={len(passed):>4}  blocked={blocked:>4}  ({el:.0f}s)")
    print(f"  收益:{total:>8.2f}%  胜率:{wr:>5.1f}%   Sharpe:{sh:>6.3f}  回撤:{md:>7.2f}%")
    print(f"  均盈:{aw:>6.2f}%  均亏:{al:>6.2f}%   PF:{pf:>5.2f}   SL:{sl} TP:{tp} time:{ti}")