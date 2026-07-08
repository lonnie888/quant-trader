"""Historical gainer scanner.

For each day in the last N days, compute the top-K USDT-perpetuals by 24h % change.
We do this by pulling daily klines for the entire market and computing
close / open - 1 per day. Each top-K entry becomes a candidate "pump event" for backtesting.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.fetcher.binance_client import BinanceClient  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scan_history")


def daily_ohlcv(client: BinanceClient, symbol: str, start_ms: int, end_ms: int) -> list[list]:
    """Pull 1d OHLCV between two timestamps. Returns list of [ts, o, h, l, c, v]."""
    out: list[list] = []
    cursor = start_ms
    tf_ms = 24 * 3600 * 1000
    while cursor < end_ms:
        batch = client.exchange.fetch_ohlcv(symbol, timeframe="1d", since=cursor, limit=1000)
        if not batch:
            break
        for row in batch:
            if row[0] >= end_ms:
                return out
            out.append(row)
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + tf_ms
        time.sleep(client.exchange.rateLimit / 1000)
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--top", type=int, default=10)
    p.add_argument("--min-quote-vol-m", type=float, default=20.0,
                   help="min 24h quote volume in millions USDT (uses 1d kline volume * close as proxy)")
    p.add_argument("--out", default="reports/gainers_history.json")
    args = p.parse_args()

    settings = load_settings(args.config)
    uni_cfg = settings.universe
    bin_cfg = settings.binance

    client = BinanceClient(api_key=bin_cfg.api_key, api_secret=bin_cfg.api_secret,
                           testnet=bool(bin_cfg.testnet))
    try:
        # get all USDT perpetuals
        markets = client.exchange.load_markets()
        symbols = sorted([s for s, m in markets.items()
                          if m.get("swap") and m.get("linear") and s.endswith(f"/USDT:USDT")])
        # filter excluded
        excl = {s.upper() for s in (uni_cfg.exclude or [])}
        symbols = [s for s in symbols if s.upper() not in excl]
        log.info("found %d USDT perpetuals", len(symbols))

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)

        # collect daily klines for each symbol
        all_daily: dict[str, list[list]] = {}
        for i, sym in enumerate(symbols, 1):
            try:
                rows = daily_ohlcv(client, sym, start_ms, end_ms)
                if rows:
                    all_daily[sym] = rows
            except Exception as e:
                log.warning("skip %s: %s", sym, e)
            if i % 20 == 0:
                log.info("progress %d/%d symbols", i, len(symbols))
                time.sleep(0.5)

        # build day -> top list
        # collect all unique day-timestamps
        day_ts = sorted({row[0] for rows in all_daily.values() for row in rows})
        log.info("days: %d", len(day_ts))

        top_by_day: dict[str, list[dict]] = {}
        for ts in day_ts:
            # pct = (close - prev_close) / prev_close for THIS day, using close of `ts` and close of day before
            candidates = []
            for sym, rows in all_daily.items():
                # find row with this ts, and prev close (row before with smaller ts)
                r = next((x for x in rows if x[0] == ts), None)
                if not r:
                    continue
                prev_close = None
                for x in rows:
                    if x[0] < ts:
                        prev_close = x[4]  # close
                if not prev_close or prev_close <= 0:
                    continue
                pct = (r[4] - prev_close) / prev_close  # using 1d kline close - prev close
                if r[4] <= 0:
                    continue
                # 24h quote volume proxy: close * volume
                qvol = r[4] * r[5]
                if qvol < args.min_quote_vol_m * 1_000_000:
                    continue
                candidates.append({"symbol": sym, "pct": pct, "qvol": qvol, "close": r[4]})
            candidates.sort(key=lambda c: c["pct"], reverse=True)
            day_str = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            top_by_day[day_str] = candidates[:args.top]

        # collect unique symbols
        all_picked = sorted({c["symbol"] for day in top_by_day.values() for c in day})
        log.info("unique symbols across %d days: %d", len(top_by_day), len(all_picked))

        # write
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "days": top_by_day,
            "unique_symbols": all_picked,
            "generated_at": datetime.utcnow().isoformat() + "Z",
        }, indent=2), encoding="utf-8")
        log.info("wrote %s", out_path)
        for day in sorted(top_by_day.keys())[-5:]:
            syms = [c["symbol"] for c in top_by_day[day]]
            log.info("  %s: %s", day, syms)
    finally:
        client.close()


if __name__ == "__main__":
    main()