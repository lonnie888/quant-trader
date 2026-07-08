"""Batch-fetch 15m OHLCV + funding for symbols in gainers_history.json.

For each symbol, we fetch the last `days` of 15m data (default 7) so that
the pump_pullback / other strategies have enough bars to detect patterns.
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
from quant_trader.data.fetcher.ohlcv_downloader import download_many  # noqa: E402
from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fetch_events")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--gainers", default="reports/gainers_history.json")
    p.add_argument("--days", type=int, default=7, help="days of 15m data per symbol")
    p.add_argument("--min-occurrences", type=int, default=1, help="only fetch symbols that appeared >= N times in gainers history")
    p.add_argument("--timeframes", nargs="*", default=["15m"])
    p.add_argument("--include-funding", action="store_true", default=True)
    args = p.parse_args()

    settings = load_settings(args.config)
    bin_cfg = settings.binance
    data_cfg = settings.data

    # read gainers history
    with open(args.gainers, "r", encoding="utf-8") as f:
        gh = json.load(f)

    # filter symbols by occurrence
    from collections import Counter
    cnt = Counter()
    for day_list in gh["days"].values():
        for c in day_list:
            cnt[c["symbol"]] += 1
    selected = sorted([s for s, n in cnt.items() if n >= args.min_occurrences])
    log.info("selected %d symbols (min_occurrences=%d)", len(selected), args.min_occurrences)

    if not selected:
        log.error("no symbols selected; lower --min-occurrences")
        return

    client = BinanceClient(api_key=bin_cfg.api_key, api_secret=bin_cfg.api_secret,
                           testnet=bool(bin_cfg.testnet))
    try:
        store = ParquetStore(data_cfg.storage_dir)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=args.days)
        log.info("fetching %d days of %s data for %d symbols", args.days, args.timeframes, len(selected))

        # process in chunks to keep memory + log volume reasonable
        chunk = 10
        for i in range(0, len(selected), chunk):
            batch = selected[i: i + chunk]
            log.info("chunk %d/%d: %s", i // chunk + 1, (len(selected) + chunk - 1) // chunk, batch)
            try:
                data_map = download_many(
                    client,
                    symbols=batch,
                    timeframes=args.timeframes,
                    lookback_days=args.days,
                    include_funding=args.include_funding,
                )
            except Exception as e:
                log.warning("chunk failed: %s", e)
                continue
            for sym, per_tf in data_map.items():
                for tf, df in per_tf.items():
                    if df.empty:
                        log.warning("empty data for %s %s", sym, tf)
                        continue
                    store.save(sym, tf, df)
                    log.info("saved %s %s: %d rows (%s -> %s)", sym, tf, len(df),
                             df.index.min().strftime("%Y-%m-%d %H:%M"),
                             df.index.max().strftime("%Y-%m-%d %H:%M"))
            time.sleep(0.5)
    finally:
        client.close()


if __name__ == "__main__":
    main()