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
import time
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


# Module-level trade cooldown set (stop_loss prevention)
# Persisted to file to survive daemon restarts
import json
from pathlib import Path

_COOLDOWN_FILE = Path("reports/paper/cooldown.json")
_COOLDOWN_SYMBOLS: dict = {}  # sym_short -> expiry_timestamp (UTC)

def _load_cooldowns():
    """Load cooldown symbols from file."""
    global _COOLDOWN_SYMBOLS
    try:
        if _COOLDOWN_FILE.exists():
            data = json.loads(_COOLDOWN_FILE.read_text())
            now = time.time()
            _COOLDOWN_SYMBOLS = {}
            for sym, expiry in data.items():
                if expiry > now:
                    _COOLDOWN_SYMBOLS[sym] = expiry
            if _COOLDOWN_SYMBOLS:
                log.info("loaded %d cooldowns from file", len(_COOLDOWN_SYMBOLS))
    except Exception:
        _COOLDOWN_SYMBOLS = {}

def _save_cooldowns():
    """Save cooldown symbols to file."""
    try:
        _COOLDOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COOLDOWN_FILE.write_text(json.dumps(_COOLDOWN_SYMBOLS, indent=2))
    except Exception:
        pass

def _is_cooldown(sym: str) -> bool:
    """Check if symbol is in cooldown (and clean expired)."""
    global _COOLDOWN_SYMBOLS
    now = time.time()
    expired = [s for s, t in _COOLDOWN_SYMBOLS.items() if t <= now]
    if expired:
        for s in expired:
            del _COOLDOWN_SYMBOLS[s]
        _save_cooldowns()
    return sym in _COOLDOWN_SYMBOLS

def _add_cooldown(sym_short: str, hours: int = 24):
    """Add symbol to trade cooldown set (avoid re-entry after SL)."""
    expiry = time.time() + hours * 3600
    _COOLDOWN_SYMBOLS[sym_short] = expiry
    _save_cooldowns()
    log.info("cooldown added %s (until %s)", sym_short,
             time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(expiry)))
# Daily circuit breaker: when tripped, no new trades today
_CIRCUIT_BROKEN_DATE: str = ""  # UTC date string when circuit broke
_CIRCUIT_SESSION_START: float = 0.0  # timestamp when daemon started (in UTC seconds)
_POSITIONS_PATH = None  # set by _refresh_watchlist on each cycle

def _check_circuit_breaker(settings=None, broker=None) -> bool:
    """Check if daily loss limit has been tripped. Returns True if trading should stop.
    Only active in demo (real money) mode.
    Uses REAL account balance change (not leveraged trade %) to avoid false triggers."""
    from datetime import datetime, timezone
    global _CIRCUIT_BROKEN_DATE
    # Only activate circuit breaker in demo (real money) mode
    if settings is not None:
        mode = getattr(settings.demo_trading, "mode", "paper")
        if mode != "demo":
            return False  # paper mode, no circuit breaker
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _CIRCUIT_BROKEN_DATE == today:
        return True  # already tripped today

    # Use REAL account balance change since session start (robust vs leverage)
    try:
        import hmac, hashlib, requests
        from quant_trader.execution.broker import FAPI_BASE_V2
        if broker is None or not hasattr(broker, "api_key"):
            return False
        ts = int(datetime.now().timestamp() * 1000)
        params = {"timestamp": str(ts), "recvWindow": "10000"}
        q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        proxies = {"http": broker.proxy, "https": broker.proxy} if getattr(broker, "proxy", None) else None
        r = requests.get(f"{FAPI_BASE_V2}/account?{q}&signature={sig}",
                         headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10)
        if r.status_code != 200:
            return False
        acct = r.json()
        wallet = float(acct.get("totalWalletBalance", 0) or 0)
        # Session start balance: initial since daemon start
        initial = getattr(settings, '_session_initial_balance', None)
        if initial is None:
            initial = wallet
            settings._session_initial_balance = wallet
        # Realized loss % of account equity since session start
        loss_pct = (wallet - initial) / initial if initial > 0 else 0.0
        log.info("CB: real balance %.2f vs session-start %.2f (loss %.2f%%)",
                 wallet, initial, loss_pct * 100)
        if loss_pct <= -0.60:  # account lost 60% of equity
            _CIRCUIT_BROKEN_DATE = today
            log.warning("CIRCUIT BREAKER TRIPPED: account loss %.2f%% <= -60%%", loss_pct * 100)
            return True
    except Exception as ex:
        log.warning("CB: real balance check failed (%s), fallback to paper pnl", ex)
        # Fallback to paper ledger pnl (scaled)
        from quant_trader.execution.paper_ledger import get_all_positions
        all_events = get_all_positions()
        session_start_dt = datetime.fromtimestamp(_CIRCUIT_SESSION_START, tz=timezone.utc) if _CIRCUIT_SESSION_START else None
        session_realized = 0.0
        for ev in all_events:
            if ev.get("status") != "closed":
                continue
            exit_ts = ev.get("exit_ts", "")
            if not exit_ts:
                continue
            try:
                exit_dt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
                if session_start_dt and exit_dt < session_start_dt:
                    continue
            except Exception:
                continue
            d = exit_dt.strftime("%Y-%m-%d")
            if d != today:
                continue
            # Scale leveraged % by margin fraction (20% of equity) to get account-level impact
            pnl_lev = ev.get("pnl_pct_lev", 0.0) or 0.0
            session_realized += pnl_lev * 0.20  # each position uses 20% equity
        if session_realized <= -0.60:
            _CIRCUIT_BROKEN_DATE = today
            log.warning("CIRCUIT BREAKER TRIPPED (paper fallback): loss %.2f%% <= -60%%", session_realized * 100)
            return True
    return False

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

            # CIRCUIT BREAKER CHECK: skip the whole strategy loop if daily loss exceeded
            if _check_circuit_breaker(settings, broker):
                log.warning("circuit breaker active: skipping this watchlist cycle")
                await _force_close_all_on_circuit(broker, positions_path)
                # Still send a feishu card to notify user
                try:
                    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
                    fw = getattr(settings.notify, "feishu_webhook", None)
                    feishu = FeishuNotifier(webhook_url=fw)
                    gainer_pairs = [(g.symbol.split("/")[0].split(":")[0], float(g.pct_change_24h)) for g in gainers]
                    card = FeishuCardBuilder.make_daily_summary(
                        as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                        gainers=gainer_pairs,
                        accepted=0, blocked=0,
                        open_pos=0,
                        opened_symbols=[],
                        blocked_list=[("⚠️ 熔断器", "今日亏损达限，已暂停交易")],
                    )
                    feishu.send_card(card)
                except Exception:
                    pass
                if refresh_event is not None:
                    refresh_event.set()
                await asyncio.sleep(900)
                continue

            # 当前循环已开仓的币种（防止同一循环内重复开单）
            _cycle_opened_syms: set = set()
            
            # Run strategy on each symbol
            from quant_trader.strategy.generator.auto_strategy import generate_instances
            from quant_trader.execution.paper_ledger import get_all_positions, get_open_positions, open_position, _has_open, evaluate_risk
            from quant_trader.data.storage.parquet_store import ParquetStore
            from datetime import datetime, timezone, timedelta

            instances = generate_instances("config/strategies.yaml")
            positions_path = Path("reports/paper/positions.jsonl")
            # Global reference for circuit breaker
            global _POSITIONS_PATH
            _POSITIONS_PATH = positions_path
            risk_cfg = settings.risk
            risk_check = {
                "initial_capital": float(settings.backtest.initial_capital),
                "max_position_pct": float(risk_cfg.max_position_pct),
                "max_total_exposure": float(risk_cfg.max_total_exposure),
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
            for ev in get_all_positions(positions_path):
                if ev.get("status") == "closed" and ev.get("exit_reason") == "time":
                    ts = ev.get("exit_ts")
                    if ts:
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            if (now - dt).total_seconds() < 3600:
                                cooldown_syms.add(ev["symbol"])
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
                except Exception as ex:
                    log.warning("下载K线失败 %s: %s, 跳过该币种", api_sym, ex)

                # 拉新失败则跳过该币种（缓存数据可能不包含最新价格，导致追高）
                if df is None or df.empty or len(df) < 100:
                    continue
                if _is_cooldown(sym.split("/")[0].split(":")[0]):
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
                                            # 回测等价行为：信号触发立即开单（有_has_open防重复）
                                            # 不回测：顺序遍历，0→1时开仓，持仓中_has_open跳过
                                                                    # 额外检查：最近12根K线内必须有 ≥13% 的泵
                        # 防止"持仓延续"信号在下跌趋势中误开仓
                        pump_window = 12
                        pump_threshold = 0.13
                        if len(df) >= pump_window:
                            # 用最高价/窗口起点收盘价计算泵（避免下影线虚高）
                            win_high = df["high"].iloc[-pump_window:].max()
                            base_close = df["close"].iloc[-pump_window]
                            pump_pct = win_high / base_close - 1 if base_close > 0 else 0
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
                        # 检查是否已有同币种持仓（防止重复开单）
                        from quant_trader.execution.paper_ledger import _has_open
                        if sym in _cycle_opened_syms or _has_open(all_events, sym):
                            log.info("跳过 %s: 已有同币种持仓", sym)
                            blocked += 1
                            blocked_list.append((sym.split("/")[0].split(":")[0], "已有持仓"))
                            continue
                        # 实盘模式下，优先用真实账户持仓数判断上限
                        if hasattr(broker, "is_real") and broker.is_real:
                            try:
                                from quant_trader.execution.real_account import get_realtime_summary
                                real = get_realtime_summary(broker.api_key, broker.secret, broker.proxy)
                                real_pos_count = real.get("positionCount", 0)
                                max_conc = int(risk_check.get("max_concurrent", 3))
                                if real_pos_count >= max_conc:
                                    allowed = False
                                    reason = "max_concurrent"
                                    log.info("实盘已达持仓上限(%d), 跳过 %s", real_pos_count, sym)
                                else:
                                    allowed = True  # 覆盖paper账本的检查
                                    reason = ""
                            except Exception:
                                # fallback: 用paper账本检查
                                allowed, reason = evaluate_risk(all_events, **risk_check)
                        else:
                            allowed, reason = evaluate_risk(all_events, **risk_check)
                        if not allowed:
                            blocked += 1
                            reason_zh = {
                                "max_concurrent": "已达持仓上限",
                                "max_total_exposure": "总敞口超限",
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
                            _cycle_opened_syms.add(sym)
                            log.info("✅ [watchlist] open %s @ %.6f id=%d", sym, entry_price, ev.id)
                        else:
                            pass  # enter failed, log already emitted by broker
            # CIRCUIT BREAKER CHECK: if daily loss exceeded, skip new trades
            if _check_circuit_breaker(settings, broker):
                log.warning("circuit breaker active: skipping new entries this cycle")
                # Force close all open positions
                await _force_close_all_on_circuit(broker, positions_path)
                if opened == 0:
                    pass  # fall through to next cycle
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
        except Exception as ex:
            log.warning("watchlist refresh failed: %s", ex)
        # Signal positions_report task that a refresh cycle is complete
        if refresh_event is not None:
            refresh_event.set()
        # 对齐K线收盘时间（每15分钟整点），收盘后立即检查开单，不固定等900秒
        try:
            import time as _time
            now = _time.time()
            # 下一个15分钟整点（00:00,00:15,00:30,00:45）
            next_15min = (int(now) // 900 + 1) * 900
            wait = next_15min - now
            await asyncio.sleep(max(wait, 1))
        except Exception:
            await asyncio.sleep(900)


async def _positions_report_loop(settings, stop_event, watchlist_event: asyncio.Event, broker=None):
    """Send positions check card to Feishu when watchlist refresh completes.
    Uses real account data when available, falls back to paper ledger."""
    from datetime import datetime, timezone, timedelta
    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
    from pathlib import Path
    import requests as sync_req
    
    PROXY = getattr(settings, "proxy", None)
    positions_path = Path("reports/paper/positions.jsonl")

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(watchlist_event.wait(), timeout=300.0)
            watchlist_event.clear()
        except asyncio.TimeoutError:
            continue
        if stop_event.is_set():
            break
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            
            # Try real account data first
            use_real = False
            real_data = None
            if broker is not None and hasattr(broker, "api_key") and broker.api_key:
                try:
                    from quant_trader.execution.real_account import get_realtime_summary
                    real_data = get_realtime_summary(broker.api_key, broker.secret, broker.proxy)
                    use_real = True
                except Exception as ex:
                    log.warning("positions report: real data fetch failed (%s), fallback to paper", ex)
            
            if use_real and real_data and real_data.get("positionCount", 0) > 0:
                # Build card from real account data
                equity = real_data.get("totalWalletBalance", 1)
                
                # Read paper ledger for entry times (to compute remaining bars)
                from quant_trader.execution.paper_ledger import get_all_positions
                paper_events = get_all_positions(positions_path)
                paper_entries = {}
                for ev in paper_events:
                    if ev.get("status") == "open":
                        sym = ev["symbol"].split("/")[0].split(":")[0] + "USDT"
                        paper_entries[sym] = {
                            "entry_ts": ev.get("entry_ts", ""),
                            "hold_bars": int(ev.get("params", {}).get("hold_bars", 48)),
                        }
                
                positions_data = []
                total_unrealized_usdt = 0.0
                for pos in real_data.get("positions", []):
                    sym = pos["symbol"]
                    entry = pos["entry"]
                    mark = pos["mark"]
                    lev = pos.get("leverage", 5)
                    # Leveraged PnL % = price change % * leverage
                    pnl_pct = (mark - entry) / entry if entry > 0 else 0
                    pnl_lev = pnl_pct * lev
                    pnl_usdt = pos.get("unrealizedPnl", 0)
                    total_unrealized_usdt += pnl_usdt
                    
                    # Compute remaining bars from paper ledger entry time
                    rb = -1
                    if sym in paper_entries:
                        pe = paper_entries[sym]
                        entry_ts = pe.get("entry_ts", "")
                        if entry_ts:
                            try:
                                ed = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                                if ed.tzinfo is None:
                                    ed = ed.replace(tzinfo=timezone.utc)
                                now = datetime.now(timezone.utc)
                                elapsed_bars = int((now - ed).total_seconds() / (15 * 60))
                                rb = max(0, pe["hold_bars"] - elapsed_bars)
                            except Exception:
                                pass
                    
                    positions_data.append({
                        "symbol": sym,
                        "entry_price": entry,
                        "last_close": mark,
                        "pnl_pct_lev": pnl_lev,
                        "remaining_bars": rb,
                        "max_favorable_pct": 0.0,
                        "max_adverse_pct": 0.0,
                    })
                
                # Fetch today's realized PnL from Binance
                import calendar, hmac, hashlib
                today_realized = 0.0
                ts = int(datetime.now().timestamp() * 1000)
                start_of_day = calendar.timegm(time.strptime(today, "%Y-%m-%d")) * 1000
                params = {"timestamp": str(ts), "recvWindow": "10000", "limit": "500", "startTime": str(start_of_day)}
                q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
                sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
                proxies = {"http": PROXY, "https": PROXY} if PROXY else None
                r = sync_req.get(
                    f"https://fapi.binance.com/fapi/v1/income?{q}&signature={sig}",
                    headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10
                )
                if r.status_code == 200:
                    for inc in r.json():
                        if isinstance(inc, dict) and inc.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                            today_realized += float(inc.get("income", 0))
                
                # Convert to percentages for display
                unrealized_pct = total_unrealized_usdt / equity * 100 if equity > 0 else 0
                realized_pct = today_realized / equity * 100 if equity > 0 else 0
                profitable_count = sum(1 for p in positions_data if p["pnl_pct_lev"] > 0)
                
                card = FeishuCardBuilder.make_positions_check(
                    today=today,
                    total_unrealized_pct=unrealized_pct,
                    total_realized_pct=realized_pct,
                    open_count=real_data["positionCount"],
                    closed_count=0,
                    profitable=profitable_count,
                    positions=positions_data,
                )
                fw = getattr(settings.notify, "feishu_webhook", None)
                FeishuNotifier(webhook_url=fw).send_card(card)
                log.info("positions report sent (real account): %d open", real_data["positionCount"])
            else:
                # Fallback to paper ledger
                from quant_trader.execution.paper_ledger import get_all_positions
                all_events = get_all_positions(positions_path)
                closed_ids = set()
                for ev in all_events:
                    if ev.get("status") in ("closed", "blocked"):
                        closed_ids.add(int(ev["id"]))
                open_pos = [ev for ev in all_events if ev.get("status") == "open" and int(ev["id"]) not in closed_ids]
                
                realized_today = 0.0
                for ev in all_events:
                    if ev.get("status") == "closed" and ev.get("exit_ts", "").startswith(today):
                        realized_today += ev.get("pnl_pct_lev", 0.0) or 0.0
                
                positions_data = []
                total_unrealized = 0.0
                for ev in open_pos:
                    entry = float(ev["entry_price"])
                    pnl_pct = 0.0
                    lev = float(ev.get("leverage", 3.0))
                    pnl_lev = pnl_pct * lev
                    total_unrealized += pnl_lev
                    remaining_bars = int(ev["params"].get("hold_bars", 24))
                    entry_ts = ev.get("entry_ts", "")
                    if entry_ts:
                        try:
                            ed = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
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
                        "last_close": 0,
                        "pnl_pct_lev": pnl_lev,
                        "remaining_bars": remaining_bars,
                        "max_favorable_pct": 0.0,
                        "max_adverse_pct": 0.0,
                    })
                
                total_closed = sum(1 for ev in all_events if ev.get("status") == "closed")
                profitable = sum(1 for p in positions_data if p["pnl_pct_lev"] > 0)
                
                if len(open_pos) == 0:
                    log.info("positions report skipped: 0 open positions")
                    _save_equity_snapshot(broker)
                    continue
                
                card = FeishuCardBuilder.make_positions_check(
                    today=today,
                    total_unrealized_pct=total_unrealized * 100,
                    total_realized_pct=realized_today * 100,
                    open_count=len(open_pos),
                    closed_count=total_closed,
                    profitable=profitable,
                    positions=positions_data,
                )
                fw = getattr(settings.notify, "feishu_webhook", None)
                FeishuNotifier(webhook_url=fw).send_card(card)
                log.info("positions report sent (paper ledger): %d open", len(open_pos))
        except Exception as ex:
            log.warning("positions report failed: %s", ex)


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
        except Exception as ex:
            log.warning("daily recap failed: %s", ex)


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
            import requests as _req
            # 用 requests + 线程池（aiohttp 代理需要 python-socks，未安装导致超时）
            def _fetch():
                proxies = {"http": PROXY, "https": PROXY} if PROXY else None
                r = _req.get(FAPI_TICKER, proxies=proxies, timeout=10)
                r.raise_for_status()
                return r.json()
            loop = asyncio.get_event_loop()
            tickers = await loop.run_in_executor(None, _fetch)
        except Exception as ex:
            log.warning("rest poll price fetch failed: %s [%s]", ex, type(ex).__name__)
            return

        price_map = {p["symbol"]: float(p["price"]) for p in tickers}
        all_events = get_all_positions(positions_path)
        open_pos = []
        closed_ids = set()
        for ev in all_events:
            if ev.get("status") in ("closed", "blocked"):
                closed_ids.add(int(ev["id"]))
        for ev in all_events:
            if ev.get("status") == "open" and int(ev["id"]) not in closed_ids:
                open_pos.append(ev)
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
        except Exception as ex:
            log.warning("rest poll error: %s", ex)
        await asyncio.sleep(15)


def _save_equity_snapshot(broker):
    """Save total equity snapshot when no positions are open."""
    try:
        import json, hmac, hashlib, requests as _req, time as _t
        from pathlib import Path
        from quant_trader.execution.broker import FAPI_BASE_V2, DemoBroker
        if not isinstance(broker, DemoBroker):
            return
        ts = int(_t.time() * 1000)
        params = {"timestamp": str(ts), "recvWindow": "10000"}
        q = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
        sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        proxy = broker.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = _req.get(f"{FAPI_BASE_V2}/account?{q}&signature={sig}",
                     headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10)
        if r.status_code == 200:
            acct = r.json()
            total = float(acct.get("totalWalletBalance", 0))
            snap = {"totalWalletBalance": total, "timestamp": _t.time(), "date": _t.strftime("%Y-%m-%d %H:%M:%S", _t.gmtime())}
            Path("reports/paper/equity.json").write_text(json.dumps(snap, indent=2))
            log.info("equity snapshot saved: %.2f USDT", total)
    except Exception as ex:
        log.warning("equity snapshot failed: %s", ex)


async def _sync_demo_positions_on_startup(broker):
    """On startup, close any demo positions whose paper ledger entry is already closed.
    This handles the case where a previous daemon died and left demo positions open.
    """
    try:
        import hmac, hashlib, requests as _req, time as _t
        from quant_trader.execution.broker import FAPI_BASE, FAPI_BASE_V2, DemoBroker
        if not isinstance(broker, DemoBroker):
            return
        from quant_trader.execution.paper_ledger import get_all_positions
        from pathlib import Path
        positions_path = Path("reports/paper/positions.jsonl")
        all_ledger = get_all_positions(positions_path)
        # Get open positions by symbol
        open_by_sym = {}
        for ev in all_ledger:
            if ev.get("status") == "open":
                sym = ev["symbol"].split("/")[0].split(":")[0] + "USDT"
                open_by_sym[sym] = True
        # Get demo positions
        proxy = broker.proxy
        proxies = {"http": proxy, "https": proxy} if proxy else None
        ts = int(_t.time() * 1000)
        params = {"timestamp": str(ts), "recvWindow": "10000"}
        q = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
        sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{FAPI_BASE}/positionRisk?{q}&signature={sig}"
        r = _req.get(f"{FAPI_BASE_V2}/positionRisk?{q}&signature={sig}", headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10)
        data = r.json()
        if not isinstance(data, list):
            log.warning("startup sync: unexpected response type %s", str(data)[:100])
            return
        closed = 0
        for p in data:
            amt = float(p.get("positionAmt", 0))
            sym = p["symbol"]
            if amt == 0:
                continue
            if sym not in open_by_sym:
                log.warning("startup sync: closing orphan demo position %s qty=%s", sym, amt)
                try:
                    ts = int(_t.time() * 1000)
                    params = {
                        "symbol": sym, "side": "SELL" if amt > 0 else "BUY",
                        "positionSide": "LONG" if amt > 0 else "SHORT",
                        "type": "MARKET", "quantity": str(abs(int(amt))),
                        "timestamp": str(ts), "recvWindow": "10000",
                    }
                    q = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
                    sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
                    url = f"{FAPI_BASE}/order?{q}&signature={sig}"
                    r2 = _req.post(url, headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10)
                    log.info("startup sync: closed %s -> %s", sym, r2.status_code)
                    closed += 1
                except Exception as ex:
                    log.warning("startup sync close failed %s: %s", sym, ex)
                # Cancel algo orders
                try:
                    ts = int(_t.time() * 1000)
                    params = {"symbol": sym, "timestamp": str(ts), "recvWindow": "10000"}
                    q = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
                    sig = hmac.new(broker.secret.encode(), q.encode(), hashlib.sha256).hexdigest()
                    _req.delete(f"{FAPI_BASE}/algoOpenOrders?{q}&signature={sig}", headers={"X-MBX-APIKEY": broker.api_key}, proxies=proxies, timeout=10)
                except Exception:
                    pass
        if closed:
            log.info("startup sync: closed %d orphan demo positions", closed)
        else:
            log.info("startup sync: no orphan demo positions found")
    except Exception as ex:
        log.warning("startup sync error: %s", ex)


async def _force_close_all_on_circuit(broker, positions_path=None):
    if positions_path is None:
        positions_path = _POSITIONS_PATH or Path("reports/paper/positions.jsonl")
    """Force close all open positions when circuit breaker trips."""
    from quant_trader.execution.paper_ledger import get_all_positions, close_position
    from datetime import datetime, timezone
    try:
        all_events = get_all_positions(positions_path)
        open_ids = set()
        for ev in all_events:
            if ev.get("status") == "open":
                eid = int(ev["id"])
                if eid not in open_ids:
                    open_ids.add(eid)
                    # Close in paper
                    try:
                        close_position(
                            position_id=eid,
                            exit_ts=datetime.now(timezone.utc).isoformat(),
                            exit_price=float(ev.get("entry_price", 0)),
                            exit_reason="circuit_breaker",
                            log_path=positions_path,
                        )
                    except Exception as e:
                        log.warning("circuit close paper failed id=%d: %s", eid, e)
        # Also try to close on demo broker
        try:
            broker.exit(
                position_id=0,  # dummy
                exit_ts=datetime.now(timezone.utc).isoformat(),
                exit_price=0.0,
                exit_reason="circuit_breaker",
                log_path=positions_path,
            )
        except Exception:
            pass
        # Try individual closes by iterating events
        all_events_after = get_all_positions(positions_path)
        for ev in all_events_after:
            if ev.get("status") == "open":
                try:
                    broker.exit(
                        position_id=int(ev["id"]),
                        exit_ts=datetime.now(timezone.utc).isoformat(),
                        exit_price=float(ev.get("entry_price", 0)),
                        exit_reason="circuit_breaker",
                        log_path=positions_path,
                    )
                except Exception as e:
                    log.warning("circuit close demo failed id=%d: %s", int(ev["id"]), e)
        log.info("circuit breaker: closed all open positions")
    except Exception as e:
        log.warning("circuit force close error: %s", e)


async def main():
    # 单例检查：kill 旧 daemon 进程
    import os
    my_pid = os.getpid()
    try:
        output = os.popen(f"pgrep -f 'quant_trader.scripts.daemon' | grep -v {my_pid}").read().strip()
        if output:
            for pid in output.split():
                pid = pid.strip()
                if pid and pid.isdigit():
                    os.system(f"kill -9 {pid} 2>/dev/null")
                    log.warning("killed old daemon PID %s (SIGKILL)", pid)
    except Exception:
        pass

    settings = load_settings()
    ws = FapiWS()

    # SL/TP watcher (uses REST poll loop for mark price, WS not needed)
    sltp = SLTPWatch()

    # Feishu notifier for SL/TP close events
    from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
    feishu_webhook = getattr(settings.notify, "feishu_webhook", None)
    feishu = FeishuNotifier(webhook_url=feishu_webhook)

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
            lev = float(ev.get("leverage", 3.0))
            # 计算PnL（如果dict里没有预计算的值）
            pnl_raw = ev.get("pnl_pct_lev")
            if pnl_raw is not None:
                pnl = float(pnl_raw)
            else:
                pnl = (exit_ - entry) / entry * lev if entry > 0 else 0.0
            # 获取最大浮盈/浮亏（如果sltp tracking了）
            max_fav = float(ev.get("max_fav_pct", 0) or 0)
            max_adv = float(ev.get("max_adv_pct", 0) or 0)
            reason = ev.get("exit_reason", "")
            sym = ev.get("symbol", "")
            card = FeishuCardBuilder.make_position_close(
                symbol=sym, exit_reason=reason,
                entry_price=entry, exit_price=exit_,
                pnl_pct_lev=pnl,
                max_fav_pct=max_fav, max_adv_pct=max_adv,
            )
            feishu.send_card(card)
            log.info("feishu close notify: %s reason=%s pnl=%+.2f%%", sym, reason, pnl*100)
            # 检查是否还有持仓，没有就保存权益快照
            try:
                from quant_trader.execution.paper_ledger import get_open_positions
                if len(get_open_positions(Path("reports/paper/positions.jsonl"))) == 0:
                    _save_equity_snapshot(broker)
            except Exception:
                pass
            # 再关 demo 仓位（单独 try，失败不影响通知）
            try:
                broker.exit(
                    position_id=int(ev.get("id", 0)),
                    exit_ts=ev.get("exit_ts", datetime.now(timezone.utc).isoformat()),
                    exit_price=float(ev.get("exit_price", 0)),
                    exit_reason=ev.get("exit_reason", ""),
                    log_path=Path("reports/paper/positions.jsonl"),
                )
            except Exception as ex:
                log.warning("demo close failed %s: %s", sym, ex)
        except Exception as ex:
            log.warning("feishu SL/TP notify failed: %s", ex)

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

    # SAFETY CHECK: real money account detected
    if hasattr(broker, "is_real") and broker.is_real:
        from quant_trader.execution.broker import FAPI_BASE as _FAPI_BASE_CHECK
        log.warning("=" * 60)
        log.warning("⚠️  REAL MONEY ACCOUNT DETECTED ⚠️")
        log.warning("    Endpoint: %s", _FAPI_BASE_CHECK)
        log.warning("    Hard cap: 10 USDT per position")
        log.warning("    Circuit breaker: account loss >= 60% (real balance check)")
        log.warning("=" * 60)

    # Start REST polling and watchlist immediately (don't wait for WS)
    # Event to signal positions_report task that a watchlist refresh completed
    refresh_event = asyncio.Event()

    async def _supervised(name, coro_factory):
        """Restart task automatically on exception."""
        while not stop_event.is_set():
            try:
                await coro_factory()
            except asyncio.CancelledError:
                break
            except Exception as ex:
                log.warning("task %s died: %s, restarting in 30s", name, ex)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    pass

    # Load persisted cooldowns from file
    _load_cooldowns()

    # Reset circuit breaker on startup (don't count historical losses)
    global _CIRCUIT_BROKEN_DATE, _CIRCUIT_SESSION_START
    _CIRCUIT_BROKEN_DATE = ""
    _CIRCUIT_SESSION_START = time.time()
    log.info("circuit breaker reset on startup (session start %.0f)", _CIRCUIT_SESSION_START)

    tasks = [
        asyncio.create_task(_supervised("rest_poll", lambda: _rest_poll_loop(settings, kline_loop, sltp, stop_event)), name="rest_poll"),
        asyncio.create_task(_supervised("watchlist", lambda: _refresh_watchlist(broker, settings, refresh_event=refresh_event)), name="watchlist"),
        asyncio.create_task(_supervised("daily_recap", lambda: _daily_recap_loop(settings, stop_event)), name="daily_recap"),
        asyncio.create_task(_supervised("positions_report", lambda: _positions_report_loop(settings, stop_event, refresh_event, broker)), name="positions_report"),
    ]

    # Real account equity tracker (save snapshot every 15 min)
    if hasattr(broker, "is_real") and broker.is_real:
        async def _real_equity_loop():
            from quant_trader.execution.real_account import save_snapshot
            while not stop_event.is_set():
                try:
                    save_snapshot(broker.api_key, broker.secret, broker.proxy)
                except Exception as ex:
                    log.warning("real equity snapshot failed: %s", ex)
                await asyncio.sleep(900)  # 15 min
        tasks.append(asyncio.create_task(
            _supervised("real_equity", lambda: _real_equity_loop()), name="real_equity"
        ))

    # Sync demo with paper ledger on startup (close orphaned positions)
    await _sync_demo_positions_on_startup(broker)

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