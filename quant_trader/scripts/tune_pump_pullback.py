"""Walk-forward tuning for the pump_pullback strategy.

For every symbol with local parquet data, run the strategy across the
full available 15m kline history and simulate a trade at every entry
signal (0->1 transition). Each trade is held for `hold_bars` with
bar-internal stop-loss and optional take-profit.

This mirrors the live `daily_runner` decision path:
  - the strategy itself decides when to enter
  - we measure the strategy's hit rate on the available sample
  - we cap the sample to events that have enough forward bars to fill hold

Aggregates per variant:
  - n_triggers, win_rate, avg_pnl, total_pnl, worst_trade, score
  - score = w_return * total_pnl + w_winrate * win_rate * 5 - w_dd * |worst_trade|

The top-N variants are written to `reports/tuning/pump_pullback_top.csv`
and the recommended variant is printed to stdout.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import yaml

from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402
from quant_trader.strategy.library.pump_pullback import PumpPullbackStrategy  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tune")


def _entry_indices(signal_series: pd.Series) -> list[int]:
    if signal_series is None or signal_series.empty:
        return []
    s = signal_series.values
    out: list[int] = []
    prev = 0
    for i, v in enumerate(s):
        if v == 1 and prev == 0:
            out.append(i)
        prev = v
    return out


def _simulate_trade(df: pd.DataFrame, entry_idx: int, hold_bars: int,
                    leverage: float, stop_loss_pct: float,
                    take_profit_pct: float) -> dict:
    if entry_idx >= len(df) or entry_idx < 0:
        return {"ok": False}
    entry_price = float(df["close"].iloc[entry_idx])
    if entry_price <= 0:
        return {"ok": False}
    sl_price = entry_price * (1 - stop_loss_pct) if stop_loss_pct > 0 else None
    tp_price = entry_price * (1 + take_profit_pct) if take_profit_pct > 0 else None
    end_idx = min(entry_idx + hold_bars, len(df) - 1)
    for j in range(entry_idx + 1, end_idx + 1):
        low = float(df["low"].iloc[j])
        high = float(df["high"].iloc[j])
        if sl_price is not None and low <= sl_price:
            return {
                "ok": True, "exit_idx": j, "exit_price": sl_price,
                "exit_reason": "stop_loss",
                "pnl_pct_lev": (sl_price - entry_price) / entry_price * leverage,
                "bars_held": j - entry_idx,
            }
        if tp_price is not None and high >= tp_price:
            return {
                "ok": True, "exit_idx": j, "exit_price": tp_price,
                "exit_reason": "take_profit",
                "pnl_pct_lev": (tp_price - entry_price) / entry_price * leverage,
                "bars_held": j - entry_idx,
            }
    exit_idx = end_idx
    exit_price = float(df["close"].iloc[exit_idx])
    pnl = (exit_price - entry_price) / entry_price * leverage
    return {
        "ok": True, "exit_idx": exit_idx, "exit_price": exit_price,
        "exit_reason": "time", "pnl_pct_lev": pnl,
        "bars_held": exit_idx - entry_idx,
    }


def _expand(value) -> list:
    if isinstance(value, list):
        return list(value)
    return [value]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategies", default="config/strategies.yaml")
    p.add_argument("--data-dir", default="./data_store")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--strategy-name", default="pump_pullback")
    p.add_argument("--top", type=int, default=20)
    p.add_argument("--out", default="reports/tuning/pump_pullback_top.csv")
    p.add_argument("--max-symbols", type=int, default=0,
                   help="0 = all symbols; otherwise cap to first N (for smoke tests)")
    p.add_argument("--weight-return", type=float, default=1.0)
    p.add_argument("--weight-winrate", type=float, default=1.0)
    p.add_argument("--weight-drawdown", type=float, default=0.5)
    args = p.parse_args()

    # load strategy config
    with open(args.strategies, "r", encoding="utf-8") as f:
        scfg = yaml.safe_load(f).get("strategies", {})
    if args.strategy_name not in scfg:
        log.error("strategy %s not in %s", args.strategy_name, args.strategies)
        return
    pspace = scfg[args.strategy_name].get("params", {})
    keys = list(pspace.keys())
    value_lists = [_expand(pspace[k]) for k in keys]
    combos = list(itertools.product(*value_lists))
    log.info("strategy=%s variants=%d", args.strategy_name, len(combos))

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    leverage = float(cfg.get("backtest", {}).get("leverage", 3.0))

    store = ParquetStore(args.data_dir)
    symbols = store.list_symbols()
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]
    log.info("symbols=%d", len(symbols))

    # preload all dfs once
    dfs: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df = store.load(sym, args.timeframe)
        if len(df) >= 60:
            dfs[sym] = df
    log.info("symbols with enough data: %d", len(dfs))

    rows: list[dict] = []
    for combo in combos:
        params = dict(zip(keys, combo))
        hold_bars = int(params.get("hold_bars", 24))
        sl = float(params.get("stop_loss_pct", 0.10))
        tp = float(params.get("take_profit_pct", 0.0))
        per_event_pnl: list[float] = []
        per_event_reason: dict[str, int] = {}
        for sym, df in dfs.items():
            try:
                strat = PumpPullbackStrategy(params)
                sigs = strat.generate_signals(df)
            except Exception as e:
                log.debug("signal gen failed %s %s: %s", sym, params, e)
                continue
            for ei in _entry_indices(sigs):
                # need at least hold_bars bars after entry to evaluate
                if ei + hold_bars >= len(df):
                    continue
                sim = _simulate_trade(df, ei, hold_bars, leverage, sl, tp)
                if not sim.get("ok"):
                    continue
                per_event_pnl.append(sim["pnl_pct_lev"])
                per_event_reason[sim["exit_reason"]] = per_event_reason.get(sim["exit_reason"], 0) + 1

        n = len(per_event_pnl)
        if n == 0:
            continue
        wins = sum(1 for p in per_event_pnl if p > 0)
        win_rate = wins / n
        avg_pnl = sum(per_event_pnl) / n
        total_pnl = sum(per_event_pnl)
        worst_trade = min(per_event_pnl)
        score = (args.weight_return * total_pnl
                 + args.weight_winrate * win_rate * 5.0
                 - args.weight_drawdown * abs(worst_trade))
        rows.append({
            **params,
            "n_triggers": n,
            "win_rate": win_rate,
            "avg_pnl": avg_pnl,
            "total_pnl": total_pnl,
            "worst_trade": worst_trade,
            "sl_count": per_event_reason.get("stop_loss", 0),
            "tp_count": per_event_reason.get("take_profit", 0),
            "time_count": per_event_reason.get("time", 0),
            "score": score,
        })

    if not rows:
        log.error("no variants produced triggers; abort")
        return
    rows.sort(key=lambda r: -r["score"])
    top = rows[: args.top]
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_out = pd.DataFrame(top)
    df_out.to_csv(out_path, index=False)
    log.info("wrote top %d variants to %s", len(top), out_path)
    print()
    print("=== TOP VARIANTS ===")
    cols = ["n_triggers", "win_rate", "avg_pnl", "total_pnl", "worst_trade",
            "pump_window", "pump_threshold", "hold_bars", "cooldown",
            "stop_loss_pct", "take_profit_pct", "trigger_pct", "score"]
    cols = [c for c in cols if c in df_out.columns]
    print(df_out[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))


if __name__ == "__main__":
    main()