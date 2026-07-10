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


async def _daily_recap_loop(stop_event):
    """Trigger daily recap at 00:00 UTC (= 08:00 北京时间) each day."""
    while not stop_event.is_set():
        from quant_trader.scripts.recap import generate, send_feishu
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        # Next 00:00 UTC = 北京时间 08:00
        target = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_sec = (target - now).total_seconds()
        log.info("next daily recap at %s UTC (in %.0f sec, = 北京时间 08:00)",
                 target.isoformat(), wait_sec)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_sec)
            break
        except asyncio.TimeoutError:
            pass
        if stop_event.is_set():
            break
        try:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            out, stats = generate(date)
            ok = send_feishu(stats)
            log.info("daily recap %s: realized=%+.2f%% trades=%d feishu=%s",
                     date, stats["realized_pct"], stats["trades"], "ok" if ok else "skip")
        except Exception as e:
            log.warning("daily recap failed: %s", e)


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

    # Feishu notifier for SL/TP close events
    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
    feishu = FeishuNotifier()

    def _on_sltp_close(closed: dict):
        """Called by sltp.on_mark when a position is auto-closed."""
        try:
            ev = closed
            entry = float(ev.get("entry_price", 0))
            exit_ = float(ev.get("exit_price", 0))
            pnl = float(ev.get("pnl_pct_lev", 0) or 0)
            reason = ev.get("exit_reason", "")
            sym = ev.get("symbol", "")
            max_fav = float(ev.get("max_favorable_pct", 0) or 0)
            max_adv = float(ev.get("max_adverse_pct", 0) or 0)
            card = FeishuCardBuilder.make_position_close(
                symbol=sym, exit_reason=reason,
                entry_price=entry, exit_price=exit_,
                pnl_pct_lev=pnl,
                max_fav_pct=max_fav, max_adv_pct=max_adv,
            )
            feishu.send_card(card)
            log.info("feishu SL/TP notify: %s reason=%s pnl=%+.2f%%", sym, reason, pnl*100)
        except Exception as e:
            log.warning("feishu SL/TP notify failed: %s", e)

    sltp.on_close = _on_sltp_close

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
        asyncio.create_task(_daily_recap_loop(stop_event), name="daily_recap"),
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