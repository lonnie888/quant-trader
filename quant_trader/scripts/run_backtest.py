"""Run backtests across all locally stored symbols/timeframes, then rank."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.backtest.engine import run_backtest  # noqa: E402
from quant_trader.backtest.report import write_json, write_markdown  # noqa: E402
from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.processors.feature_engine import add_basic_indicators  # noqa: E402
from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402
from quant_trader.selection.leaderboard import build_rows, to_dataframe  # noqa: E402
from quant_trader.selection.ranker import rank  # noqa: E402
from quant_trader.strategy.generator.auto_strategy import generate_instances  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_backtest")


def _to_dict(obj):
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    return vars(obj)


def _score_test_only(metrics: dict, weights: dict) -> float:
    """Score function biased to test metrics (or fall back to all if empty)."""
    if not metrics or metrics.get("n_trades", 0) == 0:
        return -1e9
    return (
        weights.get("weight_return", 1.0) * metrics.get("total_return", 0.0)
        + weights.get("weight_sharpe", 0.0) * metrics.get("sharpe", 0.0)
        + weights.get("weight_winrate", 0.0) * metrics.get("win_rate", 0.0)
        - weights.get("weight_drawdown", 0.0) * abs(metrics.get("max_drawdown", 0.0))
        + weights.get("weight_profit_factor", 0.0) * (metrics.get("profit_factor", 1.0) - 1.0)
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategies", default="config/strategies.yaml")
    p.add_argument("--symbols", nargs="*", default=None)
    p.add_argument("--timeframes", nargs="*", default=None)
    p.add_argument("--out", default="reports/leaderboard.md")
    p.add_argument("--out-json", default="reports/leaderboard.json")
    p.add_argument("--train-test-split", type=float, default=0.7,
                   help="fraction of bars for training; rest is test (0 to disable)")
    p.add_argument("--min-test-trades", type=int, default=5,
                   help="require at least this many trades in test segment")
    args = p.parse_args()

    settings = load_settings(args.config)
    bt_cfg = settings.backtest
    data_cfg = settings.data
    scoring_cfg = settings.scoring

    store = ParquetStore(data_cfg.storage_dir)
    symbols = args.symbols or store.list_symbols()
    timeframes = args.timeframes or data_cfg.timeframes
    if not symbols:
        log.error("no symbols found in %s. Run update_data first.", data_cfg.storage_dir)
        return

    log.info("symbols=%d, timeframes=%s, train_test_split=%.2f",
             len(symbols), timeframes, args.train_test_split)
    instances = generate_instances(args.strategies)
    log.info("strategy instances: %d", len(instances))
    total_jobs = len(symbols) * len(timeframes) * len(instances)
    log.info("total backtest jobs: %d", total_jobs)

    results = []
    failed = 0
    t0 = time.time()
    job_idx = 0
    for sym in symbols:
        for tf in timeframes:
            df = store.load(sym, tf)
            if df.empty:
                log.warning("skip %s %s (no data)", sym, tf)
                continue
            df = add_basic_indicators(df)
            funding_path = Path(data_cfg.storage_dir) / sym / "funding.parquet"
            if funding_path.exists():
                funding = store.load(sym, "funding")
                if funding is not None and not funding.empty:
                    from quant_trader.data.processors.feature_engine import align_funding
                    df = align_funding(df, funding, tf)
            for name, params, strat in instances:
                job_idx += 1
                try:
                    sigs = strat.generate_signals(df)
                    res = run_backtest(
                        df, sigs, symbol=sym, timeframe=tf,
                        strategy_name=name, params=params,
                        initial_capital=float(bt_cfg.initial_capital),
                        leverage=float(bt_cfg.leverage),
                        fee_rate=float(bt_cfg.fee_rate),
                        slippage_bps=float(bt_cfg.slippage_bps),
                        use_funding=bool(bt_cfg.use_funding),
                        train_test_split=args.train_test_split,
                    )
                    results.append(res)
                except Exception as e:
                    failed += 1
                    log.warning("[%d/%d] FAIL %s %s %s: %s", job_idx, total_jobs, sym, tf, name, e)
                if job_idx % 50 == 0 or job_idx == total_jobs:
                    elapsed = time.time() - t0
                    rate = job_idx / max(elapsed, 0.1)
                    eta = (total_jobs - job_idx) / max(rate, 0.1)
                    log.info("progress: %d/%d (%.1f%%) %.1f jobs/s, ETA %.0fs, failed=%d",
                             job_idx, total_jobs, 100 * job_idx / total_jobs, rate, eta, failed)

    log.info("backtest done in %.1fs, ok=%d, failed=%d", time.time() - t0, len(results), failed)
    if not results:
        log.error("no successful backtests")
        return

    rows = build_rows(results)
    weights = _to_dict(scoring_cfg)
    weights = {k: weights[k] for k in weights if k.startswith("weight_")}
    constraints = _to_dict(scoring_cfg.constraints)

    # Annotate with train/test metrics + overfit_gap + robust scoring
    for r in rows:
        r["train_metrics"] = r.get("train_metrics", {}) or {}
        r["test_metrics"] = r.get("test_metrics", {}) or {}
        r["split_idx"] = r.get("split_idx")
        tm = r["train_metrics"]
        em = r["test_metrics"]
        # overfit_gap: positive means test > train (good); negative means train > test (suspicious)
        gap = (em.get("total_return", 0.0) if em else 0.0) - (tm.get("total_return", 0.0) if tm else 0.0)
        r["overfit_gap"] = gap
        r["test_trades"] = em.get("n_trades", 0) if em else 0
        r["test_return"] = em.get("total_return", 0.0) if em else 0.0
        r["test_sharpe"] = em.get("sharpe", 0.0) if em else 0.0
        r["test_dd"] = em.get("max_drawdown", 0.0) if em else 0.0
        r["test_winrate"] = em.get("win_rate", 0.0) if em else 0.0
        # requires: constraints pass on test, plus min test trades
        r["passes"] = (
            (em.get("n_trades", 0) >= args.min_test_trades)
            and (em.get("sharpe", 0.0) >= constraints.get("min_sharpe", 0.5))
            and (em.get("max_drawdown", 0.0) > -abs(constraints.get("max_drawdown", 0.25)))
        )
        # score = use test metrics (true out-of-sample)
        r["score"] = _score_test_only(em, weights)

    rows.sort(key=lambda x: x.get("score", -1e18), reverse=True)
    log.info("ranked %d backtests; %d pass test constraints", len(rows),
             sum(1 for r in rows if r["passes"]))

    df = to_dataframe(rows)
    csv_path = args.out.replace(".md", ".csv")
    Path(csv_path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)

    write_markdown(rows[:50], args.out, title="Quant Trader — Top 50 (test-segment scored)")
    write_json(rows, args.out_json)
    log.info("report written: %s, %s, %s", args.out, args.out_json, csv_path)


if __name__ == "__main__":
    main()