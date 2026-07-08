"""Refresh the local Parquet store from Binance USDT-M public klines.

No API key required: we hit https://fapi.binance.com/fapi/v1/klines directly,
which is the same endpoint ccxt's binanceusdm uses for public market data.

For every symbol folder in data_store, fetch the last `lookback_days` of 15m
klines ending at `as_of` (default: now UTC), and overwrite the parquet.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import requests
import pandas as pd

from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("refresh_fapi")

BASE = "https://fapi.binance.com/fapi/v1/klines"
INTERVAL = "15m"
TF_MS = 15 * 60 * 1000


def fetch_all(symbol: str, start_ms: int, end_ms: int, sleep_s: float = 0.25) -> pd.DataFrame:
    """Paginate /fapi/v1/klines from start_ms (inclusive) to end_ms (exclusive)."""
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": INTERVAL,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        for attempt in range(3):
            try:
                r = requests.get(BASE, params=params, timeout=15)
                if r.status_code == 429:
                    time.sleep(2.0)
                    continue
                r.raise_for_status()
                batch = r.json()
                break
            except Exception as e:
                log.warning("[%s] attempt %d failed: %s", symbol, attempt + 1, e)
                time.sleep(1.5)
        else:
            log.error("[%s] giving up after 3 failures", symbol)
            return pd.DataFrame()
        if not batch:
            break
        out.extend([row[:6] for row in batch])
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + TF_MS
        time.sleep(sleep_s)
    if not out:
        return pd.DataFrame()
    df = pd.DataFrame(out, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = df.set_index("timestamp")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = df[c].astype(float)
    return df


def folder_to_symbol(folder: str) -> str:
    """Convert 'THE_USDT_USDT' back to 'THE/USDT:USDT'."""
    if folder.endswith("_USDT_USDT"):
        base = folder[: -len("_USDT_USDT")]
        return f"{base}/USDT:USDT"
    return folder


def symbol_to_api(sym: str) -> str:
    return sym.split("/")[0].split(":")[0] + "USDT"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data-dir", default="./data_store")
    p.add_argument("--timeframe", default="15m")
    p.add_argument("--lookback-days", type=int, default=7)
    p.add_argument("--as-of", default=None,
                   help="YYYY-MM-DD; default = now UTC. End of day UTC is used as the cutoff.")
    p.add_argument("--symbols", default=None,
                   help="comma-separated symbol filter (ccxt form). default = all folders in data_dir")
    args = p.parse_args()

    store = ParquetStore(args.data_dir)
    all_folders = store.list_symbols()
    if args.symbols:
        wanted = set(s.strip() for s in args.symbols.split(","))
        folders = [f for f in all_folders if folder_to_symbol(f) in wanted]
    else:
        folders = all_folders
    log.info("refreshing %d symbols, lookback=%dd, timeframe=%s",
             len(folders), args.lookback_days, args.timeframe)

    if args.as_of:
        as_of_dt = datetime.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt = as_of_dt + timedelta(days=1) - timedelta(milliseconds=1)
    else:
        end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=args.lookback_days)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    log.info("window: %s -> %s", start_dt.isoformat(), end_dt.isoformat())

    ok, fail, empty = 0, 0, 0
    for i, folder in enumerate(folders, 1):
        sym = folder_to_symbol(folder)
        api_sym = symbol_to_api(sym)
        df = fetch_all(api_sym, start_ms, end_ms)
        if df.empty:
            empty += 1
            log.warning("[%d/%d] %s (%s) -> empty", i, len(folders), sym, api_sym)
            continue
        store.save(sym, args.timeframe, df)
        ok += 1
        if i % 10 == 0 or i == len(folders):
            log.info("[%d/%d] %s -> %d rows (last=%s, last_close=%.6f)",
                     i, len(folders), sym, len(df), df.index[-1].isoformat(), df["close"].iloc[-1])

    log.info("done: ok=%d empty=%d fail=%d", ok, empty, fail)


if __name__ == "__main__":
    main()