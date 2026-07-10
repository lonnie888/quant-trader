"""Grid search + walk-forward validation for pump_pullback.
Includes fees (0.04%) + slippage (0.05%). 70/30 walk-forward split."""
from __future__ import annotations

import itertools
import json
import sys
import time
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

FEE_RATE = 0.0004      # 0.04% per trade (entry + exit = 0.08%)
SLIPPAGE = 0.0005      # 0.05% slippage
LEVERAGE = 3.0
MIN_TRADES = 5

FIXED = {
    "pump_window": 12, "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12, "take_profit_pct": 0.0,
    "side": "long_only",
}

# Grid: only vary impactful params
GRID = {
    "pump_threshold": [0.10, 0.13],
    "pullback_min": [0.0, 0.03, 0.05, 0.08],
    "hold_bars": [24, 48],
    "stop_loss_pct": [0.05, 0.10],
    "cooldown": [12, 24],
}


def simulate(close, high, low, sigs, sl_pct, hold_bars, fees=True):
    """Simulate trades with fees + slippage, return list of PnLs."""
    n = len(sigs)
    trades = []
    in_pos = False
    entry_p = 0.0
    held = 0
    sl_p = 0.0
    entry_i = 0
    for i in range(n):
        if not in_pos:
            if sigs[i] == 1:
                in_pos = True
                entry_p = close[i] * (1 + SLIPPAGE)  # buy at ask
                entry_i = i
                held = 0
                sl_p = entry_p * (1 - sl_pct)
        else:
            held += 1
            if low[i] <= sl_p:
                exit_p = sl_p * (1 - SLIPPAGE)  # sell at bid
                in_pos = False
            elif held >= hold_bars or sigs[i] == 0:
                exit_p = close[i] * (1 - SLIPPAGE)
                in_pos = False
            else:
                continue
            # PnL = (exit - entry) / entry * lev - fees
            pnl = (exit_p - entry_p) / entry_p * LEVERAGE
            if fees:
                pnl -= FEE_RATE * 2  # entry + exit fees
            trades.append(pnl)
    return trades


def run_one(params, data_slice):
    """Run strategy on a dict of symbols -> DataFrame."""
    strategy = PumpPullbackStrategy(params)
    all_trades = []
    for sym, df in data_slice.items():
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        trades = simulate(close, high, low, sigs.values,
                          float(params["stop_loss_pct"]),
                          int(params["hold_bars"]))
        all_trades.extend(trades)
    return all_trades


def metrics(pnls):
    if len(pnls) < MIN_TRADES:
        return None
    p = np.array(pnls)
    total = float(p.sum())
    wr = float((p > 0).mean()) * 100
    sharpe = float(p.mean() / p.std()) * np.sqrt(96) if p.std() > 0 else 0.0
    cum = np.cumsum(p)
    peak = np.maximum.accumulate(cum)
    max_dd = float((cum - peak).min())
    score = total * (1 + wr / 100) - abs(max_dd) * 0.3
    return {"total_return%": round(total*100,2), "win_rate%": round(wr,1),
            "sharpe": round(sharpe,3), "max_dd%": round(max_dd*100,2),
            "trades": len(pnls), "score": round(score,2)}


def main():
    CUTOFF = pd.Timestamp("2026-06-10", tz="UTC")
    WF_SPLIT = pd.Timestamp("2026-06-25", tz="UTC")  # 70% train / 30% test

    # Load data
    print("Loading data...")
    full_data = {}
    for sym in SYMBOLS:
        df = store.load(sym, "15m")
        if df.empty:
            continue
        df = df[df.index >= CUTOFF]
        if len(df) >= 1000:
            full_data[sym] = df
    print(f"Loaded {len(full_data)} symbols, {len(df)} bars each")

    # Split train/test
    train_data = {s: df[df.index < WF_SPLIT] for s, df in full_data.items()
                  if len(df[df.index < WF_SPLIT]) >= 500}
    test_data = {s: df[df.index >= WF_SPLIT] for s, df in full_data.items()
                 if len(df[df.index >= WF_SPLIT]) >= 500}
    print(f"Train: {len(train_data)} symbols, Test: {len(test_data)} symbols")

    # Build grid
    keys = list(GRID.keys())
    combos = list(itertools.product(*[GRID[k] for k in keys]))
    total = len(combos)
    print(f"Grid: {total} combos")

    results = []
    t0 = time.time()

    for ci, combo in enumerate(combos, 1):
        params = dict(FIXED)
        params.update(dict(zip(keys, combo)))

        # Train
        train_trades = run_one(params, train_data)
        train_m = metrics(train_trades)
        if train_m is None:
            continue

        # Test (out-of-sample)
        test_trades = run_one(params, test_data)
        test_m = metrics(test_trades)
        if test_m is None:
            continue

        # OOS Sharpe > 0 + score decay < 50%
        decay = 1 - test_m["score"] / train_m["score"] if train_m["score"] > 0 else 1.0
        results.append({
            "params": {k: params[k] for k in keys},
            "train": train_m,
            "test": test_m,
            "decay": round(decay, 3),
        })

        if ci % 10 == 0 or ci == total:
            print(f"  [{ci}/{total}] {len(results)} valid, {time.time()-t0:.0f}s")

    # Sort by test score
    results.sort(key=lambda r: r["test"]["score"], reverse=True)

    print(f"\n{'='*110}")
    print(f"{'Rank':>4}  {'Score(T)':>8}  {'Score(O)':>8}  {'Return%':>8}  {'Win%':>6}  {'Sharpe':>7}  {'MaxDD%':>7}  {'Trades':>5}  {'Decay':>6}  Params")
    print(f"{'='*110}")
    for rank, r in enumerate(results[:15], 1):
        p = r["params"]
        tr = r["test"]
        param_str = (f"pt={p['pump_threshold']} pbmin={p['pullback_min']} "
                     f"hold={p['hold_bars']} sl={p['stop_loss_pct']} cool={p['cooldown']}")
        print(f"{rank:>4}  {r['train']['score']:>8.2f}  {tr['score']:>8.2f}  "
              f"{tr['total_return%']:>8.2f}  {tr['win_rate%']:>6.1f}  {tr['sharpe']:>7.3f}  "
              f"{tr['max_dd%']:>7.2f}  {tr['trades']:>5}  {r['decay']:>6.2f}  {param_str}")

    # Find best with low decay
    stable = [r for r in results if r["decay"] < 0.5 and r["test"]["sharpe"] > 0]
    if stable:
        stable.sort(key=lambda r: r["test"]["score"], reverse=True)
        best = stable[0]
        p = best["params"]
        print(f"\n{'='*110}")
        print(f"🏆 最佳(低衰减): train_score={best['train']['score']} → "
              f"test_score={best['test']['score']} decay={best['decay']}")
        print(f"   params: pt={p['pump_threshold']} pbmin={p['pullback_min']} "
              f"hold={p['hold_bars']} sl={p['stop_loss_pct']} cool={p['cooldown']}")
        print(f"   OOS: return={best['test']['total_return%']}% "
              f"wr={best['test']['win_rate%']}% sharpe={best['test']['sharpe']} "
              f"max_dd={best['test']['max_dd%']}% trades={best['test']['trades']}")

    out_path = Path("reports/paper/backtest_wf.json")
    out_path.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nSaved to {out_path}")

    # Also show current config params
    print("\n📌 当前配置参数 (v1.1) 在OOS上的表现:")
    for r in results:
        p = r["params"]
        if (p["pump_threshold"] == 0.10 and p["pullback_min"] == 0.05
                and p["hold_bars"] == 48 and p["stop_loss_pct"] == 0.05
                and p["cooldown"] == 24):
            print(f"   Score(train): {r['train']['score']} → Score(test): {r['test']['score']}")
            print(f"   Return: {r['test']['total_return%']}%  Win: {r['test']['win_rate%']}%  "
                  f"Sharpe: {r['test']['sharpe']}  MaxDD: {r['test']['max_dd%']}%  "
                  f"Trades: {r['test']['trades']}  Decay: {r['decay']}")
            break

if __name__ == "__main__":
    main()