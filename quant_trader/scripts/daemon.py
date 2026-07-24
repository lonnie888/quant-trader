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
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.execution.broker import create_broker  # noqa: E402
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


async def _refresh_watchlist(broker, settings, top_n: int = 30,
                             refresh_event: asyncio.Event | None = None):
    """Periodic task: refresh watchlist from gainers scanner and run strategy."""
    while True:
        try:
            client = BinanceClient(api_key="", api_secret="", testnet=False)
            try:
                gainers = scan_gainers(client, quote="USDT", top_n=top_n,
                                       min_quote_volume_24h=20_000_000)
            finally:
                client.close()
            syms_ccxt = [g.symbol for g in gainers]
            if not syms_ccxt:
                await asyncio.sleep(900)
                continue
            log.info("watchlist refreshed: %d symbols", len(syms_ccxt))

            # Run strategy on each symbol
            from quant_trader.strategy.generator.auto_strategy import generate_instances
            from quant_trader.execution.paper_ledger import get_all_positions, get_open_positions, open_position, _has_open, evaluate_risk
            from quant_trader.data.storage.parquet_store import ParquetStore
            from datetime import datetime, timezone, timedelta

            instances = generate_instances("config/strategies.yaml")
            positions_path = Path("reports/paper/positions.jsonl")
            risk_cfg = settings.risk
            risk_check = {
                "initial_capital": float(settings.backtest.initial_capital),
                "max_position_pct": float(risk_cfg.max_position_pct),
                "max_total_exposure": float(risk_cfg.max_total_exposure),
                "daily_loss_limit": float(risk_cfg.daily_loss_limit),
                "max_concurrent": int(risk_cfg.max_concurrent),
            }
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            opened = 0
            opened_syms = []
            blocked = 0
            blocked_list = []
            now = datetime.now(timezone.utc)

            # 1h cooldown for timeout exits
            cooldown_syms = set()
            for e in get_all_positions(positions_path):
                if e.get("status") == "closed" and e.get("exit_reason") == "time":
                    ts = e.get("exit_ts")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - dt).total_seconds() < 3600:
                                cooldown_syms.add(e["symbol"])
                        except Exception:
                            pass

            store = ParquetStore(settings.data.storage_dir)
            FAPI_KLINE = "https://fapi.binance.com/fapi/v1/klines"

            for sym in syms_ccxt:
                if sym in cooldown_syms:
                    continue
                if _has_open(get_all_positions(positions_path), sym):
                    continue

                # 每次都拉新数据，确保策略基于最新行情判断
                df = None
                now = datetime.now(timezone.utc)
                api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                try:
                    start_ms = int((now - timedelta(days=7)).timestamp() * 1000)
                    end_ms = int(now.timestamp() * 1000)
                    url = f"{FAPI_KLINE}?symbol={api_sym}&interval=15m&startTime={start_ms}&endTime={end_ms}&limit=1000"
                    import requests as _rq
                    r = _rq.get(url, timeout=30)
                    r.raise_for_status()
                    raw = r.json()
                    if raw:
                        import pandas as _pd
                        _rows = []
                        for row in raw:
                            _rows.append({
                                "timestamp": int(row[0]), "open": float(row[1]),
                                "high": float(row[2]), "low": float(row[3]),
                                "close": float(row[4]), "volume": float(row[5]),
                            })
                        _df = _pd.DataFrame(_rows)
                        _df["timestamp"] = _pd.to_datetime(_df["timestamp"], unit="ms", utc=True)
                        _df = _df.set_index("timestamp")
                        store.save(sym, "15m", _df)
                        df = _df
                except Exception as e:
                    log.warning("下载K线失败 %s: %s, 回退缓存", api_sym, e)

                # 拉新失败时回退缓存，缓存也没有则跳过
                if df is None or df.empty or len(df) < 100:
                    df = store.load(sym, "15m")
                if df is None or df.empty or len(df) < 100:
                    continue
                if sym in _cooldown_symbols:
                    continue
                for name, params, strat in instances:
                    try:
                        sigs = strat.generate_signals(df)
                    except Exception:
                        continue
                    if sigs.empty:
                        continue
                    s = sigs.values
                    last_entry = -1
                    prev = 0
                    for i, v in enumerate(s):
                        if v == 1 and prev == 0:
                            last_entry = i
                        prev = v
                    if s[-1] == 1 and last_entry >= 0:
                        bars_since = len(s) - 1 - last_entry
                                                # 额外检查：最近12根K线内必须有 ≥13% 的泵
                        # 防止"持仓延续"信号在下跌趋势中误开仓
                        pump_window = 12
                        pump_threshold = 0.13
                        if len(df) >= pump_window:
                            win_high = df["high"].iloc[-pump_window:].max()
                            win_low = df["low"].iloc[-pump_window:].min()
                            pump_pct = win_high / win_low - 1 if win_low > 0 else 0
                            if pump_pct < pump_threshold:
                                log.warning("跳过 %s: 最近12根K线无泵(涨幅%.1f%%<13%%)", sym, pump_pct * 100)
                                blocked += 1
                                blocked_list.append((sym.split("/")[0].split(":")[0], f"无泵(涨幅{pump_pct*100:.1f}%)"))
                                continue
                        # 用最新已收盘 K 线收盘价开单，与回测一致
                        # 实时 ticker 价格可能已偏离信号 K 线，造成追高
                        entry_price = float(df.iloc[-1]["close"])
                        now_ts = datetime.now(timezone.utc).isoformat()
                        all_events = get_all_positions(positions_path)
                        allowed, reason = evaluate_risk(all_events, **risk_check)
                        if not allowed:
                            blocked += 1
                            reason_zh = {
                                "max_concurrent": "已达持仓上限",
                                "max_total_exposure": "总敞口超限",
                                "daily_loss_limit": "日亏损达限",
                            }.get(reason, reason)
                            blocked_list.append((sym.split("/")[0].split(":")[0], reason_zh))
                            continue
                        ev = broker.enter(
                            symbol=sym, strategy=name, params=params,
                            entry_ts=now_ts, entry_price=entry_price,
                            leverage=float(settings.backtest.leverage),
                            open_day=today, log_path=positions_path,
                            risk_check=risk_check,
                        )
                        if ev is not None and ev.status == "open":
                            opened += 1
                            opened_syms.append(sym.split("/")[0].split(":")[0])
                            log.info("✅ [watchlist] open %s @ %.6f id=%d", sym, entry_price, ev.id)
            if opened > 0 or blocked > 0:
                try:
                    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
                    gainer_pairs = [(g.symbol.split("/")[0].split(":")[0], float(g.pct_change_24h)) for g in gainers]
                    fw = getattr(settings.notify, "feishu_webhook", None)
                    feishu = FeishuNotifier(webhook_url=fw)
                    card = FeishuCardBuilder.make_daily_summary(
                        as_of=today, gainers=gainer_pairs,
                        accepted=opened, blocked=blocked,
                        open_pos=len(get_open_positions(positions_path)),
                        opened_symbols=opened_syms,
                        blocked_list=blocked_list,
                    )
                    feishu.send_card(card)
                except Exception:
                    pass
        except Exception as e:
            log.warning("watchlist refresh failed: %s", e)
        # Signal positions_report task that a refresh cycle is complete
        if refresh_event is not None:
            refresh_event.set()
        await asyncio.sleep(900)


async def _positions_report_loop(settings, stop_event, watchlist_event: asyncio.Event):
    """Send positions check card to Feishu when watchlist refresh completes."""
    from datetime import datetime, timezone, timedelta
    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
    from quant_trader.execution.paper_ledger import get_all_positions
    from pathlib import Path
    import requests as sync_req

    FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"
    PROXY = getattr(settings, "proxy", None)
    positions_path = Path("reports/paper/positions.jsonl")

    def _fetch_prices_sync():
        try:
            return sync_req.get(FAPI_TICKER, proxies={"http": PROXY, "https": PROXY}, timeout=10).json()
        except Exception as e:
            log.warning("positions report: ticker fetch failed: %s", e)
            return []

    while not stop_event.is_set():
        # Wait for watchlist to finish a refresh cycle
        try:
            await asyncio.wait_for(watchlist_event.wait(), timeout=300.0)
            watchlist_event.clear()
        except asyncio.TimeoutError:
            continue  # safety: fire anyway every 5 min
        if stop_event.is_set():
            break
        try:
            open_pos = []
            closed_ids = set()
            all_events = get_all_positions(positions_path)
            for e in all_events:
                if e.get("status") in ("closed", "blocked"):
                    closed_ids.add(int(e["id"]))
            for e in all_events:
                if e.get("status") == "open" and int(e["id"]) not in closed_ids:
                    open_pos.append(e)

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            realized_today = 0.0
            for e in all_events:
                if e.get("status") == "closed" and e.get("exit_ts", "").startswith(today):
                    realized_today += e.get("pnl_pct_lev", 0.0) or 0.0

            price_map = {}
            tickers = _fetch_prices_sync()
            price_map = {p["symbol"]: float(p["price"]) for p in tickers}

            positions_data = []
            total_unrealized = 0.0
            for ev in open_pos:
                api_sym = ev["symbol"].split("/")[0].split(":")[0] + "USDT"
                entry = float(ev["entry_price"])
                mark = price_map.get(api_sym, entry)
                pnl_pct = (mark - entry) / entry if entry else 0.0
                lev = float(ev.get("leverage", 3.0))
                pnl_lev = pnl_pct * lev
                total_unrealized += pnl_lev
                remaining_bars = int(ev["params"].get("hold_bars", 24))
                # Calculate actual remaining bars based on elapsed time
                entry_ts = ev.get("entry_ts", "")
                if entry_ts:
                    try:
                        ed = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                        # 兼容无时区的时间戳
                        if ed.tzinfo is None:
                            ed = ed.replace(tzinfo=timezone.utc)
                        now = datetime.now(timezone.utc)
                        elapsed_bars = int((now - ed).total_seconds() / (15 * 60))
                        remaining_bars = max(0, remaining_bars - elapsed_bars)
                    except Exception:
                        pass
                positions_data.append({
                    "symbol": ev["symbol"],
                    "entry_price": entry,
                    "last_close": mark,
                    "pnl_pct_lev": pnl_lev,
                    "remaining_bars": remaining_bars,
                    "max_favorable_pct": 0.0,
                    "max_adverse_pct": 0.0,
                })

            total_closed = sum(1 for e in all_events if e.get("status") == "closed")
            profitable = sum(1 for p in positions_data if p["pnl_pct_lev"] > 0)

            card = FeishuCardBuilder.make_positions_check(
                today=today,
                total_unrealized_pct=total_unrealized * 100,
                total_realized_pct=realized_today * 100,
                open_count=len(open_pos),
                closed_count=total_closed,
                profitable=profitable,
                positions=positions_data,
            )
            if len(open_pos) == 0:
                log.info("positions report skipped: 0 open positions")
                return
            from quant_trader.execution.notifier import FeishuNotifier
            fw = getattr(settings.notify, "feishu_webhook", None)
            FeishuNotifier(webhook_url=fw).send_card(card)
            log.info("positions report sent (after kline close): %d open", len(open_pos))
        except Exception as e:
            log.warning("positions report failed: %s", e)


async def _daily_recap_loop(settings, stop_event):
    """Trigger daily recap at 00:00 UTC (= 08:00 北京时间) each day."""
    while not stop_event.is_set():
        from quant_trader.scripts.recap import generate, send_feishu
        fw = getattr(settings.notify, "feishu_webhook", None)
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
            ok = send_feishu(stats, webhook_url=fw)
            log.info("daily recap %s: realized=%+.2f%% trades=%d feishu=%s",
                     date, stats["realized_pct"], stats["trades"], "ok" if ok else "skip")
        except Exception as e:
            log.warning("daily recap failed: %s", e)


async def _rest_poll_loop(settings, kline_loop, sltp, stop_event):
    """Fallback REST polling when WebSocket is unavailable.
    Polls mark price every 15s. Uses aiohttp to avoid blocking the event loop."""
    import aiohttp
    from quant_trader.execution.paper_ledger import get_all_positions
    from pathlib import Path

    positions_path = Path("reports/paper/positions.jsonl")
    FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/price"
    PROXY = getattr(settings, "proxy", None)

    async def _check_sltp() -> None:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(FAPI_TICKER, proxy=PROXY, timeout=aiohttp.ClientTimeout(total=10)) as r:
                    r.raise_for_status()
                    tickers = await r.json()
        except Exception as e:
            log.warning("rest poll price fetch failed: %s", e)
            return

        price_map = {p["symbol"]: float(p["price"]) for p in tickers}
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
    feishu_webhook = getattr(settings.notify, "feishu_webhook", None)
    feishu = FeishuNotifier(webhook_url=feishu_webhook)

    def _add_cooldown(sym_short: str):
        """Add symbol to trade cooldown set (avoid re-entry after SL)."""
        _cooldown_symbols.add(sym_short)
        log.info("cooldown added %s (24h skip)", sym_short)

    def _on_sltp_close(closed: dict):
        """Called by sltp.on_mark when a position is auto-closed."""
        try:
            ev = closed
            sym_short = ev.get("symbol", "").split("/")[0].split(":")[0]
            if ev.get("exit_reason") == "stop_loss":
                _add_cooldown(sym_short)
            # 先发飞书通知（即使 broker.exit 失败也要通知）
            entry = float(ev.get("entry_price", 0))
            exit_ = float(ev.get("exit_price", 0))
            pnl = float(ev.get("pnl_pct_lev", 0) or 0)
            reason = ev.get("exit_reason", "")
            sym = ev.get("symbol", "")
            card = FeishuCardBuilder.make_position_close(
                symbol=sym, exit_reason=reason,
                entry_price=entry, exit_price=exit_,
                pnl_pct_lev=pnl,
                max_fav_pct=0.0, max_adv_pct=0.0,
            )
            feishu.send_card(card)
            log.info("feishu close notify: %s reason=%s pnl=%+.2f%%", sym, reason, pnl*100)
            # 再关 demo 仓位（单独 try，失败不影响通知）
            try:
                broker.exit(
                    position_id=int(ev.get("id", 0)),
                    exit_ts=ev.get("exit_ts", datetime.now(timezone.utc).isoformat()),
                    exit_price=float(ev.get("exit_price", 0)),
                    exit_reason=ev.get("exit_reason", ""),
                    log_path=Path("reports/paper/positions.jsonl"),
                )
            except Exception as e:
                log.warning("demo close failed %s: %s", sym, e)
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

    # Create brokers (paper + demo dual-run)
    proxy = getattr(settings, "proxy", None)
    broker_paper = create_broker(settings, mode="paper")
    broker_demo = create_broker(settings, mode="demo", proxy=proxy)
    broker_mode = getattr(settings.demo_trading, "mode", "paper")
    log.info("broker mode: %s (paper+demo dual-run)", broker_mode)
    # Use paper broker for risk checks, demo for actual orders
    broker = broker_demo

    # Start REST polling and watchlist immediately (don't wait for WS)
    # Event to signal positions_report task that a watchlist refresh completed
    refresh_event = asyncio.Event()

    tasks = [
        asyncio.create_task(_rest_poll_loop(settings, kline_loop, sltp, stop_event), name="rest_poll"),
        asyncio.create_task(
            _refresh_watchlist(broker, settings, refresh_event=refresh_event),
            name="watchlist",
        ),
        asyncio.create_task(_daily_recap_loop(settings, stop_event), name="daily_recap"),
        asyncio.create_task(_positions_report_loop(settings, stop_event, refresh_event), name="positions_report"),
    ]

    # WebSocket 在当前网络环境的 daemon 中无法稳定连接（aiohttp ws_connect
    # 通过 HTTP CONNECT 代理在 asyncio 事件循环中挂死，但独立测试可通）。
    # daemon 完全由 REST 轮询 + watchlist 驱动，功能等价于 15m K 线精度。
    # 实盘部署到其他服务器后再启用 WS。
    log.info("WebSocket disabled (incompatible with proxy in this environment).")
    log.info("REST polling (15s SL/TP) + watchlist (15min kline) active.")

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