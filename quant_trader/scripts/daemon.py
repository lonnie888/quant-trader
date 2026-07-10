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
import signal
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.realtime.ws_client import FapiWS, stream_mark  # noqa: E402
from quant_trader.data.realtime.kline_strategy import KlineStrategyLoop  # noqa: E402
from quant_trader.data.realtime.mark_stream import MarkPriceStream  # noqa: E402
from quant_trader.data.realtime.sltp_watch import SLTPWatch  # noqa: E402
from quant_trader.data.fetcher.gainers_scanner import scan_gainers  # noqa: E402
from quant_trader.data.fetcher.binance_client import BinanceClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("daemon")

# Top 30 most active USDT-perps as the default watchlist.
# In a more advanced version, refresh from gainers scanner every N minutes.
DEFAULT_WATCHLIST = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "TRXUSDT", "DOTUSDT", "MATICUSDT", "LINKUSDT",
    "LTCUSDT", "BCHUSDT", "NEARUSDT", "ATOMUSDT", "UNIUSDT", "APTUSDT",
    "ARBUSDT", "OPUSDT", "INJUSDT", "TIAUSDT", "SUIUSDT", "SEIUSDT",
    "ORDIUSDT", "WLDUSDT", "BLURUSDT", "PYTHUSDT", "JTOUSDT", "JUPUSDT",
]


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


async def _refresh_sltp_subs(mark: MarkPriceStream, sltp: SLTPWatch, ws: FapiWS):
    """Re-subscribe SL/TP watch to currently open positions."""
    while True:
        try:
            symbols = sltp.refresh_open()
            new = []
            for sym in symbols:
                # 提取出 USDT 形式
                api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                if not sltp.is_subscribed(api_sym):
                    sltp.mark_subscribed(api_sym)
                    new.append(api_sym)
            if new:
                streams = [stream_mark(s) for s in new]
                await ws.subscribe(streams)
                for s in streams:
                    ws.on(s, lambda data, sym=api_sym: mark._handle(data))  # type: ignore
                log.info("sltp subscribed: %d new symbols", len(new))
        except Exception as e:
            log.warning("sltp refresh error: %s", e)
        await asyncio.sleep(30)


async def main():
    settings = load_settings()
    ws = FapiWS()

    # mark price stream: shared between SL/TP watch and strategy
    mark = MarkPriceStream(ws, [])

    # SL/TP watcher
    sltp = SLTPWatch()

    async def on_mark_tick(sym, mp):
        # Dispatch to SL/TP watcher
        sltp.on_mark(sym, mp)

    # Strategy loop on kline close
    kline_loop = KlineStrategyLoop(ws, settings=settings)
    kline_loop.set_mark_provider(mark.get)

    # initial: subscribe to default watchlist 15m kline
    await kline_loop.subscribe(DEFAULT_WATCHLIST, interval="15m")

    # SL/TP watch handler registration
    async def on_mark_for_sltp(sym, mp):
        sltp.on_mark(sym, mp)
    mark._on_update = on_mark_for_sltp

    # Background tasks
    tasks = [
        asyncio.create_task(ws.run(), name="ws"),
        asyncio.create_task(_refresh_watchlist(ws, kline_loop, sltp, settings), name="watchlist"),
        asyncio.create_task(_refresh_sltp_subs(mark, sltp, ws), name="sltp_refresh"),
    ]

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
            # Windows doesn't support add_signal_handler
            pass

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