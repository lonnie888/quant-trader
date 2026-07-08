"""Replay pump_pullback signals on a past day using gainers_history.json
plus live Binance public klines (no auth needed for /fapi/v1/klines).

Steps:
  1. load top-10 gainers for `--as-of` from reports/gainers_history.json
  2. fetch 2 days of 15m klines ending at as-of (public fapi)
  3. apply the locked pump_pullback variant
  4. for every bar where the strategy was long at the END of the window,
     fetch forward klines and simulate hold+SL/TP
  5. write a markdown recap
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests
import pandas as pd
import yaml

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.fetcher.gainers_scanner import Gainer  # noqa: E402
from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402
from quant_trader.strategy.library.pump_pullback import PumpPullbackStrategy  # noqa: E402
from quant_trader.scripts.recap import simulate_hold  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("replay_day")


def fetch_klines(symbol: str, start_ms: int, end_ms: int = 0) -> pd.DataFrame:
    """Public fapi endpoint, no auth needed for klines."""
    out = []
    cursor = start_ms
    api_symbol = symbol.split("/")[0].split(":")[0] + "USDT"
    base = "https://fapi.binance.com/fapi/v1/klines"
    while end_ms == 0 or cursor < end_ms:
        params = {"symbol": api_symbol, "interval": "15m", "startTime": cursor, "limit": 1000}
        r = requests.get(base, params=params, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        # fapi returns 12 columns; keep only [ts,o,h,l,c,v]
        out.extend([row[:6] for row in batch])
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 15 * 60 * 1000
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategies", default="config/strategies.yaml")
    p.add_argument("--gainers-file", default="reports/gainers_history.json")
    p.add_argument("--as-of", required=True, help="YYYY-MM-DD")
    p.add_argument("--symbols", default=None,
                   help="comma-separated ccxt symbols; bypasses gainers_history.json")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    settings = load_settings(args.config)
    bt_cfg = settings.backtest
    leverage = float(bt_cfg.leverage)

    with open(args.strategies, "r", encoding="utf-8") as f:
        scfg = yaml.safe_load(f).get("strategies", {})
    pp_params = scfg["pump_pullback"]["params"]
    flat = {k: (v[0] if isinstance(v, list) else v) for k, v in pp_params.items()}
    log.info("locked params: %s", flat)

    if args.symbols:
        wanted = [s.strip() for s in args.symbols.split(",")]
        day_top = [{"symbol": s, "pct": 0, "qvol": 0, "close": 0} for s in wanted]
    else:
        with open(args.gainers_file, "r", encoding="utf-8") as f:
            gh = json.load(f)
        day_top = gh["days"].get(args.as_of, [])
        if not day_top:
            log.error("no gainers for %s", args.as_of)
            return
    gainers = [
        Gainer(symbol=c["symbol"], last=c.get("close", 0), pct_change_24h=c["pct"] * 100, quote_volume_24h=c["qvol"])
        for c in day_top[:10]
    ]
    log.info("%s gainers: %s", args.as_of, [g.symbol for g in gainers])

    as_of_dt = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if args.symbols:
        # for live (today) replay, end = now, start = 2 days back
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=2)
    else:
        end = as_of_dt + timedelta(days=1) - timedelta(milliseconds=1)
        start = end - timedelta(days=2)

    store = ParquetStore(settings.data.storage_dir)
    rows: list[dict] = []
    for g in gainers:
        try:
            df = fetch_klines(g.symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        except Exception as e:
            log.warning("fetch failed %s: %s", g.symbol, e)
            continue
        if df.empty:
            log.warning("no klines for %s", g.symbol)
            continue
        df = df[df.index <= end]
        if len(df) < 60:
            log.warning("not enough bars for %s (%d)", g.symbol, len(df))
            continue
        store.save(g.symbol, "15m", df)
        try:
            sigs = PumpPullbackStrategy(flat).generate_signals(df)
        except Exception as e:
            log.warning("signal failed %s: %s", g.symbol, e)
            continue
        if int(sigs.iloc[-1]) != 1:
            continue
        entry_idx = None
        s = sigs.values
        prev = 0
        for i, v in enumerate(s):
            if v == 1 and prev == 0:
                entry_idx = i
                break
            prev = v
        if entry_idx is None:
            continue
        entry_price = float(df["close"].iloc[entry_idx])
        entry_ts = df.index[entry_idx]
        # fetch forward starting ONE bar after entry
        fetch_start = int(entry_ts.timestamp() * 1000) + 15 * 60 * 1000
        try:
            forward = fetch_klines(g.symbol, fetch_start, 0)
        except Exception as e:
            log.warning("forward fetch failed %s: %s", g.symbol, e)
            continue
        if forward.empty:
            continue
        # fwd is a DataFrame indexed by datetime; reset_index keeps the
        # timestamp as the first column so simulate_hold can compare against
        # entry_ts_ms. fapi klines have 12 cols; we already keep [ts,o,h,l,c,v].
        fwd = forward.reset_index().values.tolist()
        fwd = [[r[0].value if hasattr(r[0], "value") else r[0]] + list(r[1:]) for r in fwd]
        sim = simulate_hold(
            fwd,
            int(entry_ts.timestamp() * 1000),
            entry_price,
            int(flat.get("hold_bars", 24)),
            leverage,
            stop_loss_pct=float(flat.get("stop_loss_pct", 0.10)),
            take_profit_pct=float(flat.get("take_profit_pct", 0.0)),
        )
        if not sim.get("ok"):
            continue
        rows.append({
            "symbol": g.symbol,
            "entry_ts": entry_ts.isoformat(),
            "entry_price": entry_price,
            "since_entry_bars": len(df) - 1 - entry_idx,
            **sim,
        })

    if not rows:
        log.warning("no trades to replay for %s", args.as_of)
        return

    n = len(rows)
    wins = sum(1 for r in rows if r["win"])
    win_rate = wins / n if n else 0
    total = sum(r["pnl_pct_lev"] for r in rows)
    avg = total / n if n else 0

    out_path = Path(args.out) if args.out else Path(f"reports/paper/replay-{args.as_of}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Replay - {args.as_of}",
        "",
        f"_Locked params: {flat}_",
        "",
        f"**Overall**: n={n}  win_rate={win_rate*100:.1f}%  avg_pnl={avg*100:+.2f}%  total_pnl={total*100:+.2f}%",
        "",
        "| symbol | entry_ts | entry | since_entry | exit | reason | pnl% (lev) | max_fav% | max_adv% | win |",
        "| --- | --- | ---: | ---: | ---: | :---: | ---: | ---: | ---: | :---: |",
    ]
    for r in sorted(rows, key=lambda x: -x["pnl_pct_lev"]):
        lines.append("| %s | %s | %.6f | %d | %.6f | %s | %+.2f | %+.2f | %+.2f | %s |" % (
            r["symbol"], r["entry_ts"], r["entry_price"], r["since_entry_bars"],
            r["exit_price"], r["exit_reason"], r["pnl_pct_lev"] * 100,
            r["max_favorable_pct"] * 100, r["max_adverse_pct"] * 100,
            "W" if r["win"] else "L",
        ))
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %s", out_path)


if __name__ == "__main__":
    main()