"""Compare: all entries vs first-entry-only per symbol."""
from __future__ import annotations

import sys, time
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

BASE = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.30,
    "side": "long_only",
}
LEV = 3.0
FEE = 0.0004
SLIP = 0.0005
MIN_BARS = 1000
DAILY_LOSS_LIMIT = 0.30

all_syms = store.list_symbols()
data = {}
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        data[sym] = df
print(f"Loaded {len(data)} symbols")

def run(max_entries=0):
    """max_entries=0: unlimited, >0: max entries per symbol."""
    strategy = PumpPullbackStrategy(BASE)
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
        tp_p = 0.0
        entry_count = 0
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if max_entries > 0 and entry_count >= max_entries:
                        continue
                    in_pos = True
                    ep = close[i] * (1 + SLIP)
                    ei = i
                    sl_p = ep * (1 - BASE["stop_loss_pct"])
                    tp_p = ep * (1 + BASE["take_profit_pct"]) if BASE["take_profit_pct"] > 0 else float("inf")
                    entry_count += 1
            else:
                if low[i] <= sl_p:
                    pnl = (sl_p * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                    all_raw.append({"pnl": pnl*100, "reason": "sl", "day": str(idx[ei])[:10], "sym": sym})
                    in_pos = False
                elif tp_p != float("inf") and high[i] >= tp_p:
                    pnl = (tp_p * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                    all_raw.append({"pnl": pnl*100, "reason": "tp", "day": str(idx[ei])[:10], "sym": sym})
                    in_pos = False
                else:
                    held = i - ei
                    if held >= BASE["hold_bars"] or s[i] == 0:
                        pnl = (close[i] * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                        all_raw.append({"pnl": pnl*100, "reason": "time", "day": str(idx[ei])[:10], "sym": sym})
                        in_pos = False
    all_raw.sort(key=lambda x: x["day"])
    daily = {}
    passed = []
    for t in all_raw:
        if daily.get(t["day"], 0) <= -DAILY_LOSS_LIMIT:
            continue
        daily[t["day"]] = daily.get(t["day"], 0) + t["pnl"] / 100
        passed.append(t)
    return passed, all_raw

for mode in ["all", "first only", "max 2"]:
    me = {"all": 0, "first only": 1, "max 2": 2}.get(mode, 0)
    t0 = time.time()
    passed, raw = run(me)
    el = time.time() - t0
    pnls = np.array([t["pnl"] for t in passed])
    raw_pnls = np.array([t["pnl"] for t in raw])
    total = float(pnls.sum()) if len(pnls) else 0
    wr = float((pnls > 0).mean()) * 100 if len(pnls) else 0
    sh = float(pnls.mean() / pnls.std()) * np.sqrt(96) if len(pnls) > 1 and pnls.std() > 0 else 0
    cum = np.cumsum(pnls)
    md = float((cum - np.maximum.accumulate(cum)).min()) if len(cum) else 0
    aw = float(pnls[pnls > 0].mean()) if (pnls > 0).any() else 0
    al = float(pnls[pnls < 0].mean()) if (pnls < 0).any() else 0
    pf = abs(sum(pnls[pnls > 0]) / sum(pnls[pnls < 0])) if (pnls < 0).sum() != 0 else float("inf")
    
    sl = sum(1 for t in passed if t["reason"] == "sl")
    tp = sum(1 for t in passed if t["reason"] == "tp")
    ti = sum(1 for t in passed if t["reason"] == "time")
    print(f"\n{'='*70}")
    print(f"{mode:>10s}: raw={len(raw)} passed={len(passed)}  ({el:.0f}s)")
    print(f"{'='*70}")
    print(f"  收益: {total:>8.2f}%  胜率: {wr:>5.1f}%  Sharpe: {sh:.3f}")
    print(f"  回撤: {md:>8.2f}%  PF: {pf:.2f}  均盈: {aw:.2f}%  均亏: {al:.2f}%")
    print(f"  退出: SL={sl} TP={tp} time={ti}")