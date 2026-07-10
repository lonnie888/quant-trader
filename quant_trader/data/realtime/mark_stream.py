"""Realtime mark price stream - lightweight wrapper.

Returns the (symbol, mark_price, event_time) for SL/TP monitoring.
"""
from __future__ import annotations

import logging

from ..realtime.ws_client import FapiWS, stream_mark

log = logging.getLogger(__name__)


class MarkPriceStream:
    """Subscribes to <symbol>@markPrice@1s for a list of symbols."""

    def __init__(self, ws: FapiWS, symbols: list[str]):
        self.ws = ws
        self.symbols = [s.upper() for s in symbols]
        self.latest: dict[str, float] = {}
        self._update_event = None  # type: ignore

    async def start(self, on_update=None):
        self._on_update = on_update
        streams = [stream_mark(s) for s in self.symbols]
        await self.ws.subscribe(streams)
        for s in streams:
            self.ws.on(s, self._handle)

    async def _handle(self, data: dict):
        sym = data.get("s", "").upper()
        try:
            mp = float(data.get("p", 0))
        except (TypeError, ValueError):
            return
        self.latest[sym] = mp
        if self._on_update is not None:
            try:
                await self._on_update(sym, mp)
            except Exception as e:
                log.exception("mark on_update error: %s", e)

    def get(self, symbol: str) -> float | None:
        """Look up mark by ccxt symbol ('BTC/USDT:USDT') or fapi ('BTCUSDT')."""
        s = symbol.upper()
        if s in self.latest:
            return self.latest[s]
        # Try converting ccxt → fapi
        if "/" in s:
            s2 = s.split("/")[0].split(":")[0] + "USDT"
            return self.latest.get(s2)
        return None

    def set(self, symbol: str, price: float):
        """Direct setter (used by tests / fallback)."""
        self.latest[symbol.upper()] = float(price)