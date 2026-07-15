"""Walk-forward OOS validation: TP=0 vs TP=0.30.
Train: 70% (older data), Test/OOS: 30% (newer data)."""
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
    "stop_loss_pct": 0.10, "side": "long_only",
}
LEV = 3.0
FEE = 0.0004
SLIP = 0.0005
MIN_BARS = 1000
DAILY_LOSS_LIMIT = 0.30

all_syms = store.list_symbols()
data = {}
cutoff = pd.Timestamp("2026-06-25", tz="UTC")
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        train = df[df.index < cutoff]
        test = df[df.index >= cutoff]
        if len(train) >= 500 and len(test) >= 200:
            data[sym] = (train, test)
print(f"Loaded {len(data)} symbols (train+test)")

def run(tp, data_dict, key="train"):
    params = dict(BASE, take_profit_pct=tp)
    strategy = PumpPullbackStrategy(params)
    all_raw = []
    for sym, (train_df, test_df) in data_dict.items():
        df = train_df if key == "train" else test_df
        n = len(df)
        if n < 200:
            continue
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
                    pnl = (sl_p * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                    all_raw.append({"pnl": pnl * 100, "day": str(idx[ei])[:10]})
                    in_pos = False
                elif tp > 0 and high[i] >= tp_p:
                    pnl = (tp_p * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                    all_raw.append({"pnl": pnl * 100, "day": str(idx[ei])[:10]})
                    in_pos = False
                else:
                    held = i - ei
                    if held >= BASE["hold_bars"] or s[i] == 0:
                        pnl = (close[i] * (1 - SLIP) - ep) / ep * LEV - FEE * 2
                        all_raw.append({"pnl": pnl * 100, "day": str(idx[ei])[:10]})
                        in_pos = False
    all_raw.sort(key=lambda x: x["day"])
    daily = {}
    passed = []
    for t in all_raw:
        if daily.get(t["day"], 0) <= -DAILY_LOSS_LIMIT:
            continue
        daily[t["day"]] = daily.get(t["day"], 0) + t["pnl"] / 100
        passed.append(t)
    return passed

for tp_val in [0.0, 0.30]:
    print(f"\n{'='*70}")
    print(f"TP={tp_val:.0%}")
    print(f"{'='*70}")
    for key in ["train", "test"]:
        t0 = time.time()
        passed = run(tp_val, data, key)
        el = time.time() - t0
        pnls = np.array([t["pnl"] for t in passed])
        if len(pnls) == 0:
            print(f"  {key}: 0 trades")
            continue
        total = float(pnls.sum())
        wr = float((pnls > 0).mean()) * 100
        sh = float(pnls.mean() / pnls.std()) * np.sqrt(96) if pnls.std() > 0 else 0
        cum = np.cumsum(pnls)
        md = float((cum - np.maximum.accumulate(cum)).min())
        pf = abs(sum(pnls[pnls > 0]) / sum(pnls[pnls < 0])) if (pnls < 0).sum() != 0 else float("inf")
        print(f"  {key:5s}: trades={len(passed):>4} 收益={total:>8.2f}% 胜率={wr:>5.1f}%  "
              f"Sharpe={sh:.3f} 回撤={md:>6.2f}%  PF={pf:.2f}  ({el:.0f}s)")