"""Update local Parquet data store for the top gainers."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# make package importable when run as `python -m quant_trader.scripts.update_data`
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.fetcher.binance_client import BinanceClient  # noqa: E402
from quant_trader.data.fetcher.gainers_scanner import scan_gainers  # noqa: E402
from quant_trader.data.fetcher.ohlcv_downloader import download_many  # noqa: E402
from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("update_data")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--top", type=int, default=None, help="override top N")
    args = p.parse_args()

    settings = load_settings(args.config)
    bin_cfg = settings.binance
    uni_cfg = settings.universe
    data_cfg = settings.data

    client = BinanceClient(
        api_key=bin_cfg.api_key,
        api_secret=bin_cfg.api_secret,
        testnet=bool(bin_cfg.testnet),
    )
    try:
        top = args.top or uni_cfg.top_n
        gainers = scan_gainers(
            client,
            quote=uni_cfg.quote,
            top_n=top,
            min_quote_volume_24h=float(uni_cfg.min_quote_volume_24h),
            exclude=uni_cfg.exclude,
        )
        log.info("top %d gainers: %s", len(gainers), [g.symbol for g in gainers])

        store = ParquetStore(data_cfg.storage_dir)
        data_map = download_many(
            client,
            symbols=[g.symbol for g in gainers],
            timeframes=data_cfg.timeframes,
            lookback_days=int(data_cfg.lookback_days),
            include_funding=bool(data_cfg.funding_rate),
        )
        for sym, per_tf in data_map.items():
            for tf, df in per_tf.items():
                if df.empty:
                    log.warning("no data for %s %s", sym, tf)
                    continue
                store.save(sym, tf, df)
                log.info("saved %s %s: %d rows", sym, tf, len(df))
    finally:
        client.close()


if __name__ == "__main__":
    main()
