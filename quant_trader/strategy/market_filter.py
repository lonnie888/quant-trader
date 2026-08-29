"""
市场状态过滤器：
1. 周末过滤（周六周日）
2. 坏时段过滤（UTC 16-20h, 00-04h）
3. 全币种 24h 平均波动率 > 阈值时停手

数据流：
- 每 N 分钟计算一次所有币种的 24h 波动率均值
- 缓存到内存（4h 刷新一次）
- 提供 is_blocked() 给策略层调用
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


class MarketFilter:
    """市场状态过滤器：判断当前是否应该开新仓."""

    def __init__(
        self,
        store,
        vol_threshold: float = 12.0,
        refresh_seconds: int = 14400,  # 4 小时
        no_weekend: bool = True,
        no_bad_hours: bool = True,
        bad_hours: Optional[set[int]] = None,
        cache_path: Optional[Path] = None,
    ):
        self.store = store
        self.vol_threshold = vol_threshold
        self.refresh_seconds = refresh_seconds
        self.no_weekend = no_weekend
        self.no_bad_hours = no_bad_hours
        self.bad_hours = bad_hours or {0, 1, 2, 3, 16, 17, 18, 19}  # UTC
        self.cache_path = cache_path or Path("reports/paper/market_state.json")

        self._lock = threading.Lock()
        self._last_update: float = 0.0
        self._avg_volatility: float = 0.0
        self._sample_size: int = 0
        self._last_reason: str = "init"
        self._enabled = True

    def is_blocked(self, now: Optional[datetime] = None) -> tuple[bool, str]:
        """Return (blocked, reason)."""
        if not self._enabled:
            return False, "disabled"
        now = now or datetime.now(timezone.utc)
        # 1. 周末过滤
        if self.no_weekend and now.weekday() >= 5:  # 周六/周日
            return True, f"周末({now.strftime('%A')})"
        # 2. 坏时段过滤
        if self.no_bad_hours and now.hour in self.bad_hours:
            return True, f"坏时段(UTC {now.hour:02d}h)"
        # 3. 波动率过滤
        with self._lock:
            self._maybe_refresh(now)
            if self._avg_volatility > self.vol_threshold:
                return True, f"市场波动率过高({self._avg_volatility:.1f}% > {self.vol_threshold}%)"
        return False, "ok"

    def get_state(self) -> dict:
        with self._lock:
            return {
                "avg_volatility": round(self._avg_volatility, 2),
                "vol_threshold": self.vol_threshold,
                "sample_size": self._sample_size,
                "last_update": self._last_update,
                "last_reason": self._last_reason,
            }

    def _maybe_refresh(self, now: datetime) -> None:
        if self._last_update and (time.time() - self._last_update) < self.refresh_seconds:
            return
        # 重新计算
        try:
            self._compute_volatility(now)
        except Exception as ex:
            log.warning("market filter compute failed: %s", ex)
            # 出错时降级为不过滤（让其他风控生效）
            self._last_reason = f"compute error: {ex}"

    def _compute_volatility(self, now: datetime) -> None:
        """Compute 24h average volatility across local-cached symbols.

        ⚠️ 用 load_local(本地 parquet) 而非 load(pg): 否则 is_blocked 同步触发的
        全量 pg 聚合会阻塞 asyncio 事件循环数分钟 (绿联 NAS 每币 ~20s).
        """
        syms = self.store.list_symbols()
        vols = []
        cutoff = now - pd.Timedelta(hours=24)
        for sym in syms:
            try:
                df = self.store.load_local(sym, "1h")
                if df.empty or len(df) < 24:
                    continue
                # 取最近 24h 数据
                df_24h = df[df.index >= cutoff]
                if len(df_24h) < 24:
                    df_24h = df.tail(24)
                c = float(df_24h["close"].iloc[-1])
                if c <= 0:
                    continue
                h = float(df_24h["high"].max())
                lo = float(df_24h["low"].min())
                vol = (h - lo) / c * 100
                vols.append(vol)
                if len(vols) >= 20:
                    break
            except Exception:
                continue
        if vols:
            with self._lock:
                self._avg_volatility = float(np.mean(vols))
                self._sample_size = len(vols)
                self._last_update = time.time()
                self._last_reason = f"refreshed {len(vols)} symbols at {now.strftime('%Y-%m-%d %H:%M')}"
                log.info(
                    "market filter refreshed: avg_volatility=%.2f%% (%d symbols)",
                    self._avg_volatility, len(vols)
                )
        else:
            self._last_reason = "no data"
            log.warning("market filter: no symbols with valid data")

    async def run_periodic_refresh(self, stop_event: asyncio.Event) -> None:
        """Background task: refresh volatility every refresh_seconds."""
        while not stop_event.is_set():
            try:
                # 用一个独立 task 跑计算（避免阻塞）
                await asyncio.to_thread(self._compute_volatility, datetime.now(timezone.utc))
            except Exception as ex:
                log.warning("periodic market filter refresh failed: %s", ex)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.refresh_seconds)
            except asyncio.TimeoutError:
                pass
        log.info("market filter periodic refresh stopped")
