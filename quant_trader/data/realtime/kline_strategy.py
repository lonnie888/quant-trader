"""Kline stream handler - runs strategy on each closed 15m bar.

On `k.x = true` (bar closed):
  - Update parquet cache for that symbol
  - Re-load df
  - Run pump_pullback strategy
  - If sigs[-1] == 1 (current bar signal) and not in cooldown/has_open
  - Open paper position with current mark price
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Awaitable

import numpy as np
import pandas as pd

from ...config import load_settings
from ...data.storage.parquet_store import ParquetStore
from ...strategy.generator.auto_strategy import generate_instances
from ...execution.paper_ledger import (
    open_position, get_all_positions, _has_open, evaluate_risk,
)

log = logging.getLogger(__name__)


class KlineStrategyLoop:
    """Listens to kline stream and runs strategy on bar close."""

    def __init__(self, ws, settings=None, store: ParquetStore | None = None,
                 cooldown_seconds: int = 3600):
        self.settings = settings or load_settings()
        self.store = store or ParquetStore(self.settings.data.storage_dir)
        self.strategies_cfg = "config/strategies.yaml"
        self.ws = ws
        self.cooldown_seconds = cooldown_seconds
        self._subscribed: set[str] = set()
        # Cache strategy instances: yaml only read at __init__
        self._instances = generate_instances(self.strategies_cfg)

    async def subscribe(self, symbols: list[str], interval: str = "15m"):
        from ..realtime.ws_client import stream_kline
        new = []
        for s in symbols:
            key = s.upper()
            if key in self._subscribed:
                continue
            stream = stream_kline(key, interval)
            self._subscribed.add(key)
            new.append(stream)
            self.ws.on(stream, self._handle)
        if new:
            await self.ws.subscribe(new)
            log.info("kline subscribed: %d streams", len(new))

    async def _handle(self, data: dict):
        k = data.get("k", {})
        if not k:
            return
        if not k.get("x"):
            return  # bar not closed yet
        sym_raw = data.get("s", "").upper()
        interval = k.get("i", "15m")
        if sym_raw.endswith("USDT"):
            sym = sym_raw[:-4] + "/USDT:USDT"
        else:
            sym = sym_raw
        # Update cache: append this closed bar
        try:
            self._update_cache(sym, interval, k)
        except Exception as e:
            log.warning("cache update failed %s: %s", sym, e)
        # Run strategy
        try:
            self._check_signal(sym)
        except Exception as e:
            log.exception("signal check failed %s: %s", sym, e)

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
        df_existing = self.store.load(sym, interval)
        new_df = pd.DataFrame([row]).set_index("timestamp")
        if df_existing.empty:
            combined = new_df
        else:
            combined = pd.concat([df_existing[~df_existing.index.isin(new_df.index)], new_df])
            combined = combined[~combined.index.duplicated(keep="last")].sort_index()
        self.store.save(sym, interval, combined)

    def _check_signal(self, sym: str):
        instances = generate_instances(self.strategies_cfg)
        positions_path = Path("reports/paper/positions.jsonl")
        all_events = get_all_positions(positions_path)

        # 1h cooldown for timeout exits
        cooldown = set()
        now = datetime.now(timezone.utc)
        for e in all_events:
            if e.get("status") == "closed" and e.get("exit_reason") == "time":
                ts = e.get("exit_ts")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if (now - dt).total_seconds() < self.cooldown_seconds:
                            cooldown.add(e["symbol"])
                    except Exception:
                        pass

        if sym in cooldown:
            return
        if _has_open(all_events, sym):
            return

        df = self.store.load(sym, "15m")
        if df.empty or len(df) < 100:
            return

        risk_cfg = self.settings.risk
        risk_check = {
            "initial_capital": float(self.settings.backtest.initial_capital),
            "max_position_pct": float(risk_cfg.max_position_pct),
            "max_total_exposure": float(risk_cfg.max_total_exposure),
            "daily_loss_limit": float(risk_cfg.daily_loss_limit),
            "max_concurrent": int(risk_cfg.max_concurrent),
        }

        for name, params, strat in self._instances:
            try:
                sigs = strat.generate_signals(df)
            except Exception as e:
                log.warning("%s strat failed: %s", sym, e)
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
                if bars_since > 1:
                    log.warning("跳过 %s: 信号滞后 %d 根K线(追高防护)", sym, bars_since)
                    continue
                # Get current mark price from mark stream
                mark = self._mark_provider(sym) if self._mark_provider else float(df.iloc[-1]["close"])
                now_ts = datetime.now(timezone.utc).isoformat()
                today = now.strftime("%Y-%m-%d")
                ev = open_position(
                    symbol=sym, strategy=name, params=params,
                    entry_ts=now_ts, entry_price=mark,
                    leverage=float(self.settings.backtest.leverage),
                    open_day=today, log_path=positions_path,
                    risk_check=risk_check,
                )
                if ev is not None and ev.status == "open":
                    log.info("✅ [realtime] open %s @ %.6f id=%d", sym, mark, ev.id)

    def set_mark_provider(self, fn: Callable[[str], float | None]):
        self._mark_provider = fn