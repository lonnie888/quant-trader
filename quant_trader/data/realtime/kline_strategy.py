"""Kline stream handler - runs strategy on each closed 15m bar via WebSocket.

On `k.x = true` (bar closed):
  - Update parquet cache for that symbol
  - Run pump_pullback strategy
  - If signal detected (and no open position/cooldown)
  - Open real position with SL/TP via broker.enter()

Manages its own WebSocket connection internally, supporting dynamic
subscription updates (reconnects with new stream list).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from ...config import load_settings
from ...data.storage.parquet_store import ParquetStore
from ...strategy.generator.auto_strategy import generate_instances
from ...strategy.market_filter import MarketFilter
from ...execution.paper_ledger import (
    get_all_positions, _has_open, evaluate_risk,
)

log = logging.getLogger(__name__)


class KlineStrategyLoop:
    """Manages WebSocket kline subscriptions and runs strategy on bar close."""

    def __init__(self, settings=None, broker=None, store: ParquetStore | None = None,
                 cooldown_seconds: int = 3600, market_filter=None):
        self.settings = settings or load_settings()
        self.store = store or ParquetStore(self.settings.data.storage_dir)
        self.strategies_cfg = "config/strategies.yaml"
        self.broker = broker
        self.cooldown_seconds = cooldown_seconds
        self.market_filter = market_filter  # 市场状态过滤器（可选）
        self._current_symbols: list[str] = []
        self._instances = generate_instances(self.strategies_cfg)
        self._ws = None
        self._ws_task: asyncio.Task | None = None
        self._ws_connecting = False  # 正在连接中，避免重复创建 task
        self._stop = False
        self._mark_provider: Callable[[str], float | None] | None = None
        # 近期信号记录 [(timestamp, symbol, action, reason)]，action: open/blocked
        self.signal_log: list = []

    async def _preheat_data(self, symbols: list[str]):
        """为新加入的币种拉取 7 天历史 K 线数据到 store，避免信号延迟。
        异步执行，不阻塞事件循环。"""
        if not symbols:
            return
        import requests as _rq
        import pandas as _pd
        from datetime import timedelta
        import asyncio
        FAPI_KLINE = "https://fapi.binance.com/fapi/v1/klines"
        now = datetime.now(timezone.utc)

        async def _fetch_one(sym: str):
            try:
                start_ms = int((now - timedelta(days=7)).timestamp() * 1000)
                end_ms = int(now.timestamp() * 1000)
                store_sym = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym + "/USDT:USDT"
                url = f"{FAPI_KLINE}?symbol={sym}&interval=15m&startTime={start_ms}&endTime={end_ms}&limit=1000"
                loop = asyncio.get_event_loop()
                r = await loop.run_in_executor(None, lambda: _rq.get(url, timeout=30))
                r.raise_for_status()
                raw = r.json()
                if raw:
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
                    self.store.save(store_sym, "15m", _df)
                    return True
            except Exception:
                return False

        # 并发拉取，每次 10 个
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i+10]
            tasks = [_fetch_one(s) for s in batch]
            results = await asyncio.gather(*tasks)
            done = sum(1 for r in results if r)
            if done:
                log.info("preheat batch %d-%d: %d/%d ok", i, i+len(batch), done, len(batch))
            await asyncio.sleep(0)  # 让事件循环处理其他任务

    def set_mark_provider(self, fn: Callable[[str], float | None]):
        self._mark_provider = fn

    async def update_subscription(self, symbols: list[str]):
        """Update the WS subscription to match the given symbol list.
        Adds new symbols via SUBSCRIBE, removes dropped via UNSUBSCRIBE.
        Only reconnects if the WS is not connected yet."""
        from .ws_client import stream_kline

        old_set = set(self._current_symbols)
        new_set = set(symbols)
        added = new_set - old_set
        removed = old_set - new_set
        self._current_symbols = list(symbols)

        # 新增的币种注册 handler 并订阅
        if added:
            for sym in added:
                self._ws.on(sym.upper(), self._handle) if self._ws else None
            if self._ws is not None:
                from .ws_client import stream_kline
                streams = [stream_kline(s, "15m") for s in added]
                await self._ws.subscribe(streams)
                log.info("ws added %d symbols: %s", len(added), list(added)[:5])
            # 新币种立即拉历史数据，避免信号延迟
            await self._preheat_data(list(added))
            # 预热后立即补扫，不错过信号
            for sym in added:
                full_sym = f"{sym[:-4]}/USDT:USDT" if sym.endswith("USDT") else sym + "/USDT:USDT"
                try:
                    await self._check_signal(full_sym)
                except Exception:
                    pass

        # 移除的币种取消订阅
        if removed and self._ws is not None:
            streams = [stream_kline(s, "15m") for s in removed]
            await self._ws.unsubscribe(streams)
            log.info("ws removed %d symbols: %s", len(removed), list(removed)[:5])

        # 首次连接：WS 还没启动，需要创建并连接
        if self._ws is None and not self._ws_connecting and symbols:
            self._ws_connecting = True
            self._ws_task = asyncio.create_task(self._run_ws())
            log.info("ws connecting: %d symbols", len(symbols))

    async def _stop_ws(self):
        if self._ws_task is not None:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        if self._ws is not None:
            try:
                await self._ws.stop()
            except Exception:
                pass
            self._ws = None

    async def _run_ws(self):
        """Connect WebSocket with current symbol streams."""
        from .ws_client import FapiWS, stream_kline

        try:
            if not self._current_symbols:
                return

            proxy = getattr(self.settings, "proxy", None)
            streams = [stream_kline(s, "15m") for s in self._current_symbols]

            self._ws = FapiWS(proxy=proxy)
            for sym in self._current_symbols:
                self._ws.on(sym.upper(), self._handle)
            await self._ws.subscribe(streams)
            log.info("[ws] registered %d handlers, streams=%d", len(self._ws.handlers), len(streams))
            connected = await self._ws.run()
            # 连接成功后（或重连后），补扫所有币种信号
            if connected:
                log.info("ws connected, catch-up scan %d symbols", len(self._current_symbols))
                for sym in self._current_symbols:
                    # 转换格式：CAPUSDT → CAP/USDT:USDT（和 _handle 一致）
                    if sym.endswith("USDT"):
                        full_sym = f"{sym[:-4]}/USDT:USDT"
                    else:
                        full_sym = sym + "/USDT:USDT"
                    try:
                        await self._check_signal(full_sym)
                    except Exception:
                        pass
        finally:
            self._ws_connecting = False
            self._ws = None  # 连接断开后清理，让 update_subscription 重建

    async def stop(self):
        """Stop the WS connection and all tasks."""
        self._stop = True
        await self._stop_ws()

    async def _handle(self, data: dict):
        """Called on each WS message (kline stream)."""
        k = data.get("k", {})
        if not k:
            return
        sym_raw = data.get("s", "").upper()
        interval = k.get("i", "15m")
        if not k.get("x"):
            return  # bar not closed yet, skip
        log.info("[ws] 收盘 %s %s close=%s", sym_raw, interval, k.get("c"))
        if sym_raw.endswith("USDT"):
            sym = sym_raw[:-4] + "/USDT:USDT"
        else:
            sym = sym_raw
        # Update cache: append this closed bar
        try:
            self._update_cache(sym, interval, k)
        except Exception as ex:
            log.warning("cache update failed %s: %s", sym, ex)
        # Run strategy
        try:
            await self._check_signal(sym)
        except Exception as ex:
            log.exception("signal check failed %s: %s", sym, ex)

    def _update_cache(self, sym: str, interval: str, k: dict):
        ts = pd.Timestamp(k["t"], unit="ms", tz="UTC")
        row = {
            "timestamp": ts,
            "open": float(k["o"]),
            "high": float(k["h"]),
            "low": float(k["l"]),
            "close": float(k["c"]),
            "volume": float(k["v"]),
        }
        new_df = pd.DataFrame([row]).set_index("timestamp")
        df_existing = self.store.load(sym, interval)
        if df_existing.empty:
            combined = new_df
        else:
            # 确保 index 精度一致（pd.concat 在 us/ms 精度差异时可能不合并）
            combined = pd.concat([df_existing, new_df])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        self.store.save(sym, interval, combined)
        log.info("[ws] cache %s: %d rows", sym, len(combined))

    async def _check_signal(self, sym: str):
        """Run strategy on the symbol and open position if signal detected."""
        import time as _time
        instances = generate_instances(self.strategies_cfg)
        positions_path = Path("reports/paper/positions.jsonl")
        all_events = get_all_positions(positions_path)
        now = datetime.now(timezone.utc)
        now_ts = now.isoformat()
        today = now.strftime("%Y-%m-%d")
        sym_short = sym.split("/")[0].split(":")[0]

        # 1. 数据准备
        df = self.store.load(sym, "15m")
        if df.empty or len(df) < 100:
            return
        if len(df) >= 2:
            gap = (df.index[-1] - df.index[-2]).total_seconds() / 60
            if gap > 30:
                return
        last_time = df.index[-1]
        if hasattr(last_time, 'tz') and last_time.tz:
            data_age = (now - last_time).total_seconds() / 60
        else:
            data_age = 999
        if data_age > 30:
            # 数据过旧，尝试从 Binance 拉取最新数据
            try:
                import requests as _rq
                from datetime import timedelta
                api_sym = sym_short + "USDT"
                url = f"https://fapi.binance.com/fapi/v1/klines?symbol={api_sym}&interval=15m&startTime={int((now-timedelta(days=7)).timestamp()*1000)}&endTime={int(now.timestamp()*1000)}&limit=1000"
                r = _rq.get(url, timeout=30)
                if r.status_code == 200:
                    raw = r.json()
                    if raw:
                        _rows = []
                        for row in raw:
                            _rows.append({"timestamp": int(row[0]), "open": float(row[1]),
                                          "high": float(row[2]), "low": float(row[3]),
                                          "close": float(row[4]), "volume": float(row[5])})
                        _df = pd.DataFrame(_rows)
                        _df["timestamp"] = pd.to_datetime(_df["timestamp"], unit="ms", utc=True)
                        _df = _df.set_index("timestamp")
                        self.store.save(sym, "15m", _df)
                        df = _df
                        log.info("[ws] %s 数据已重新拉取(%d rows)", sym, len(df))
            except Exception:
                pass
            if df.empty or len(df) < 100:
                return

        # 2. 泵检测
        pump_window, pump_threshold = 12, 0.13
        if len(df) >= pump_window:
            win_high = df["high"].iloc[-pump_window:].max()
            base_close = df["close"].iloc[-pump_window]
            pump_pct = win_high / base_close - 1 if base_close > 0 else 0
            if pump_pct < pump_threshold:
                log.info("[ws] %s 无泵(%.1f%% < 13%%)", sym, pump_pct * 100)
                return
        else:
            return

        # 3. 策略信号检测
        has_signal = False
        sig_name = None
        sig_params = None
        for name, params, strat in self._instances:
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
                has_signal = True
                sig_name = name
                sig_params = params
                break
        if not has_signal:
            return

        entry_price = float(df.iloc[-1]["close"])

        # 4. 有信号 → 检查阻挡原因
        # 4a. 1h cooldown
        for ev in all_events:
            if ev.get("status") == "closed" and ev.get("exit_reason") == "time":
                ts = ev.get("exit_ts")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if (now - dt).total_seconds() < self.cooldown_seconds:
                            self.signal_log.append((_time.time(), sym_short, "blocked", "冷却中"))
                            return
                    except Exception:
                        pass

        # 4b. 已有持仓
        if _has_open(all_events, sym):
            self.signal_log.append((_time.time(), sym_short, "blocked", "已有持仓"))
            return

        # 4c. 24h 止损冷却
        try:
            cool_file = Path("reports/paper/cooldown.json")
            if cool_file.exists():
                import json as _json
                data = _json.loads(cool_file.read_text())
                expiry = data.get(sym_short, 0)
                if expiry > _time.time():
                    self.signal_log.append((_time.time(), sym_short, "blocked", "冷却中"))
                    return
        except Exception:
            pass

        # 4c. 市场状态过滤（周末/坏时段/波动率）
        if self.market_filter is not None:
            blocked, reason = self.market_filter.is_blocked(now)
            if blocked:
                log.info("⏸ [ws] market filter skip %s: %s", sym_short, reason)
                self.signal_log.append((_time.time(), sym_short, "blocked", f"市场过滤: {reason}"))
                return

        # 4d. 风控检查
        risk_cfg = self.settings.risk
        risk_check = {
            "initial_capital": float(self.settings.backtest.initial_capital),
            "max_position_pct": float(risk_cfg.max_position_pct),
            "max_total_exposure": float(risk_cfg.max_total_exposure),
            "max_concurrent": int(risk_cfg.max_concurrent),
        }
        allowed, reason = evaluate_risk(all_events, **risk_check)
        if not allowed:
            log.info("✅ [ws] skip %s: %s", sym, reason)
            self.signal_log.append((_time.time(), sym_short, "blocked", reason))
            return

        # 5. 开仓
        if self.broker is not None:
            ev = self.broker.enter(
                symbol=sym, strategy=sig_name, params=sig_params,
                entry_ts=now_ts, entry_price=entry_price,
                leverage=float(self.settings.backtest.leverage),
                open_day=today, log_path=positions_path,
                risk_check=risk_check,
            )
        else:
            from ...execution.paper_ledger import open_position
            ev = open_position(
                symbol=sym, strategy=sig_name, params=sig_params,
                entry_ts=now_ts, entry_price=entry_price,
                leverage=float(self.settings.backtest.leverage),
                open_day=today, log_path=positions_path,
                risk_check=risk_check,
            )
        if ev is not None and ev.status == "open":
            log.info("✅ [ws] open %s @ %.6f id=%d", sym, entry_price, ev.id)
            self.signal_log.append((_time.time(), sym_short, "open", ""))