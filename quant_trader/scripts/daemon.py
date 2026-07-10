"""Quant Trader daemon — 常驻进程，替代 cron 三套。

Tasks:
  1. WebSocket connection (kline 1m/15m + markPrice)
  2. Strategy loop on bar close
  3. SL/TP watch on mark ticks
  4. Daily recap at 02:00 UTC (optional)

Usage:
  python -m quant_trader.scripts.daemon
"""
from __future__ import annotations

import asyncio
import logging
import requests
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.realtime.ws_client import FapiWS, stream_kline  # noqa: E402
from quant_trader.data.realtime.kline_strategy import KlineStrategyLoop  # noqa: E402
from quant_trader.data.realtime.sltp_watch import SLTPWatch  # noqa: E402
from quant_trader.data.fetcher.gainers_scanner import scan_gainers  # noqa: E402
from quant_trader.data.fetcher.binance_client import BinanceClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("daemon")

# Initial empty watchlist. _refresh_watchlist task will populate from
# gainers scanner on first iteration (top N by 24h quote volume).
# Avoids subscribing to stale/hardcoded symbols that may not exist.
DEFAULT_WATCHLIST: list[str] = []


async def _refresh_watchlist(ws, kline_loop: KlineStrategyLoop, sltp: SLTPWatch,
                             settings, top_n: int = 30):
    """Periodic task: refresh watchlist from gainers scanner."""
    while True:
        try:
            client = BinanceClient(api_key="", api_secret="", testnet=False)
            try:
                gainers = scan_gainers(client, quote="USDT", top_n=top_n,
                                       min_quote_volume_24h=20_000_000)
            finally:
                client.close()
            syms = [g.symbol.split("/")[0].split(":")[0] + "USDT" for g in gainers]
            if syms:
                await kline_loop.subscribe(syms, interval="15m")
                log.info("watchlist refreshed: %d symbols", len(syms))
        except Exception as e:
            log.warning("watchlist refresh failed: %s", e)
        # Refresh every 15 min
        await asyncio.sleep(900)


async def _rest_poll_loop(settings, kline_loop, sltp, stop_event):
    """Fallback REST polling when WebSocket is unavailable.
    Polls mark price every 15s."""
    from quant_trader.execution.paper_ledger import get_all_positions
    from pathlib import Path

    positions_path = Path("reports/paper/positions.jsonl")
    FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"

    async def _check_sltp():
        """Check SL/TP for all open positions via REST ticker prices."""
        all_events = get_all_positions(positions_path)
        open_pos = []
        closed_ids = set()
        for e in all_events:
            if e.get("status") in ("closed", "blocked"):
                closed_ids.add(int(e["id"]))
        for e in all_events:
            if e.get("status") == "open" and int(e["id"]) not in closed_ids:
                open_pos.append(e)

        if not open_pos:
            return

        # Fetch all tickers in one batch
        try:
            r = requests.get(FAPI_TICKER, timeout=10)
            r.raise_for_status()
            price_map = {p["symbol"]: float(p["price"]) for p in r.json()}
        except Exception as e:
            log.warning("rest poll price fetch failed: %s", e)
            return

        for ev in open_pos:
            api_sym = ev["symbol"].split("/")[0].split(":")[0] + "USDT"
            mark = price_map.get(api_sym)
            if mark is None:
                continue
            sltp.on_mark(ev["symbol"], mark)

    while not stop_event.is_set():
        try:
            await _check_sltp()
        except Exception as e:
            log.warning("rest poll error: %s", e)
        await asyncio.sleep(15)


async def main():
    settings = load_settings()
    ws = FapiWS()

    # SL/TP watcher (uses REST poll loop for mark price, WS not needed)
    sltp = SLTPWatch()

    # Strategy loop on kline close
    kline_loop = KlineStrategyLoop(ws, settings=settings)

    # initial: subscribe to default watchlist 15m kline
    await kline_loop.subscribe(DEFAULT_WATCHLIST, interval="15m")

    # Graceful shutdown
    stop_event = asyncio.Event()
    def _on_signal():
        log.info("shutdown signal received")
        stop_event.set()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    # Start REST polling and watchlist immediately (don't wait for WS)
    tasks = [
        asyncio.create_task(_rest_poll_loop(settings, kline_loop, sltp, stop_event), name="rest_poll"),
        asyncio.create_task(_refresh_watchlist(ws, kline_loop, sltp, settings), name="watchlist"),
    ]

    # Try WebSocket in the background (may take ~15s through proxy)
    log.info("connecting to WebSocket in background (fstream.binance.com)...")
    async def _try_ws():
        connected = await ws.run(stop_event=stop_event)
        if connected:
            log.info("✅ WebSocket online — kline strategy running on bar close")
        else:
            log.info("ℹ️ WebSocket unavailable — kline strategy disabled, REST polling active")
    tasks.append(asyncio.create_task(_try_ws(), name="ws_try"))

    log.info("daemon started: watchlist=%d symbols", len(DEFAULT_WATCHLIST))
    try:
        await stop_event.wait()
    finally:
        log.info("stopping daemon...")
        await ws.stop()
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        log.info("daemon stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass