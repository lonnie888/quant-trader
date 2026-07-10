"""Faster backtest: last 1 month data, 3 values per param."""
from __future__ import annotations

import sys
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

SYMBOLS = [
    "DEXE/USDT:USDT","THE/USDT:USDT","BASED/USDT:USDT","KAT/USDT:USDT",
    "HMSTR/USDT:USDT","TLM/USDT:USDT","ARPA/USDT:USDT","NOM/USDT:USDT",
    "EVAA/USDT:USDT","UAI/USDT:USDT","VANRY/USDT:USDT","US/USDT:USDT",
    "SENT/USDT:USDT","TAC/USDT:USDT","TAG/USDT:USDT","VELVET/USDT:USDT",
    "ALLO/USDT:USDT",
]

CUTOFF = pd.Timestamp("2026-06-10", tz="UTC")
LEVERAGE = 3.0
MIN_TRADES = 3

FIXED = {
    "pump_window": 12, "pump_threshold": 0.15,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.85, "vol_recover": 1.1,
    "trigger_pct": 0.003, "ema_period": 9,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.0, "side": "long_only",
}

# Pre-load + trim to last 1 month
print("Loading data (last 1 month)...")
data = {}
for sym in SYMBOLS:
    df = store.load(sym, "15m")
    if df.empty:
        continue
    df = df[df.index >= CUTOFF]
    if len(df) >= 1000:
        data[sym] = df
print(f"Loaded {len(data)} symbols, ~{len(df)} bars each")


def run_one(params) -> dict | None:
    strategy = PumpPullbackStrategy(params)
    all_trades = []
    for sym, df in data.items():
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        n = len(s)
        in_pos = False
        entry_p = 0.0
        held = 0
        sl_p = 0.0
        hold_bars = int(params.get("hold_bars", 24))
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    in_pos = True; entry_p = close[i]; held = 0
                    sl_p = entry_p * (1 - float(params.get("stop_loss_pct", 0.10)))
            else:
                held += 1
                if low[i] <= sl_p:
                    exit_p = sl_p; in_pos = False
                elif held >= hold_bars or s[i] == 0:
                    exit_p = close[i]; in_pos = False
                else:
                    continue
                all_trades.append((exit_p - entry_p) / entry_p * LEVERAGE)

    if len(all_trades) < MIN_TRADES:
        return None
    pnls = np.array(all_trades)
    total = float(pnls.sum())
    wr = float((pnls > 0).mean()) * 100
    sharpe = float(pnls.mean() / pnls.std()) * np.sqrt(96) if pnls.std() > 0 else 0.0
    cum = np.cumsum(pnls); peak = np.maximum.accumulate(cum)
    max_dd = float((cum - peak).min())
    score = total * (1 + wr / 100) - abs(max_dd) * 0.3
    return {"total_return%": round(total*100,2), "win_rate%": round(wr,1),
            "sharpe": round(sharpe,3), "max_dd%": round(max_dd*100,2),
            "trades": len(all_trades), "score": round(score,2)}


def test(param_name, values, fmt):
    print(f"\n{'='*70}")
    print(f"▶ {param_name}")
    print(f"{'='*70}")
    print(f"{'值':>10}  {'Return%':>8}  {'Win%':>6}  {'Sharpe':>7}  {'MaxDD%':>7}  {'Trades':>6}  {'Score':>7}")
    print("-"*70)
    results = []
    for v in values:
        p = dict(FIXED)
        p[param_name] = v
        r = run_one(p)
        if r is None:
            print(f"{fmt(v):>10}  {'n/a':>8}  {'n/a':>6}  {'n/a':>7}  {'n/a':>7}  {'n/a':>6}  {'n/a':>7}")
            continue
        results.append((v, r))
        print(f"{fmt(v):>10}  {r['total_return%']:>8.2f}  {r['win_rate%']:>6.1f}  "
              f"{r['sharpe']:>7.3f}  {r['max_dd%']:>7.2f}  {r['trades']:>6}  {r['score']:>7.2f}")
    if results:
        results.sort(key=lambda x: x[1]["score"], reverse=True)
        print(f"  🏆 最佳: {param_name}={fmt(results[0][0])}")
    return results


# 1. pump_window
test("pump_window", [8, 12, 16], lambda v: f"{v}根")
# 2. pump_threshold
test("pump_threshold", [0.10, 0.13, 0.15, 0.20], lambda v: f"{v*100:.0f}%")
# 3. hold_bars
test("hold_bars", [12, 24, 36, 48], lambda v: f"{v}根({v*15//60}h)")
# 4. stop_loss_pct
test("stop_loss_pct", [0.05, 0.10, 0.15], lambda v: f"{v*100:.0f}%")
# 5. cooldown
test("cooldown", [6, 12, 24], lambda v: f"{v}根({v*15//60}h)")
# 6. vol_shrink
test("vol_shrink", [0.80, 0.85, 0.90], lambda v: f"{v:.2f}")
# 7. vol_recover
test("vol_recover", [1.0, 1.1, 1.2], lambda v: f"{v:.1f}")
# 8. trigger_pct
test("trigger_pct", [0.0, 0.003, 0.005], lambda v: f"{v*100:.1f}%")
# 9. ema_period
test("ema_period", [7, 9, 12], lambda v: f"{v}")

print("\n✅ 全部完成")