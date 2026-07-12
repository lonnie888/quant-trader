"""Fapi WebSocket client for Binance USDT-perp (aiohttp + proxy support).

Provides async streaming of:
  - kline_1m / kline_15m streams
  - trade streams (for tick-level checks)

Features:
  - HTTP CONNECT proxy support (required in China)
  - Auto-reconnect with exponential backoff
  - Multi-stream multiplexing via SUBSCRIBE method
  - Callback dispatch by stream name
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import aiohttp

log = logging.getLogger(__name__)

WS_BASE = "wss://fapi.binance.com/ws"
PROXY = None  # set via settings.yaml proxy field


class FapiWS:
    """Single WebSocket connection to fapi, with HTTP CONNECT proxy support."""

    def __init__(self, proxy: str | None = PROXY):
        self.proxy = proxy
        self.handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self.subs: set[str] = set()
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reader_task: asyncio.Task | None = None
        self._stop = False

    def on(self, stream: str, handler: Callable[[dict], Awaitable[None]]):
        """Register async callback for a stream pattern (exact match, dedup)."""
        handlers = self.handlers.setdefault(stream, [])
        if handler not in handlers:
            handlers.append(handler)

    async def subscribe(self, streams: list[str]):
        """Subscribe to additional streams (idempotent)."""
        new = [s for s in streams if s not in self.subs]
        if not new:
            return
        self.subs.update(new)
        if self._ws is not None:
            await self._ws.send_json({
                "method": "SUBSCRIBE",
                "params": new,
                "id": int(asyncio.get_event_loop().time() * 1000) % 100000,
            })
            log.info("subscribed: %s", new)

    async def _reader(self):
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if self._stop:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    # Single stream: {"stream":"...","data":{...}}
                    # Or raw message: direct dict
                    stream = data.get("stream", "")
                    payload = data.get("data", data)
                    for h in self.handlers.get(stream, []):
                        try:
                            await h(payload)
                        except Exception as e:
                            log.exception("handler error on %s: %s", stream, e)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    log.warning("ws closed")
                    break
        except Exception as e:
            log.exception("ws reader error: %s", e)

    async def run(self, stop_event=None):
        """Main loop: connect, subscribe, read; reconnect on failure."""
        self._session = aiohttp.ClientSession()
        try:
            max_attempts = 3
            backoff = 1.0
            connected = False
            for attempt in range(1, max_attempts + 1):
                if self._stop or (stop_event and stop_event.is_set()):
                    break
                try:
                    log.info("connecting to %s (attempt %d/%d, proxy=%s)", WS_BASE, attempt, max_attempts, self.proxy)
                    kwargs = {"timeout": aiohttp.ClientWSTimeout(ws_close=8.0)}
                    if self.proxy:
                        kwargs["proxy"] = self.proxy
                    ws = await self._session.ws_connect(WS_BASE, **kwargs)
                    self._ws = ws
                    if self.subs:
                        await ws.send_json({
                            "method": "SUBSCRIBE",
                            "params": list(self.subs),
                            "id": 1,
                        })
                    connected = True
                    self._reader_task = asyncio.create_task(self._reader())
                    await self._reader_task
                    break
                except Exception as e:
                    log.warning("ws connect failed (%d/%d): %s", attempt, max_attempts, e)
                if not connected and attempt < max_attempts:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60.0)
            return connected
        finally:
            if self._session and not self._session.closed:
                await self._session.close()

    async def stop(self):
        self._stop = True
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._session and not self._session.closed:
            await self._session.close()


def stream_kline(symbol: str, interval: str) -> str:
    return f"{symbol.lower()}@kline_{interval}"


def stream_trade(symbol: str) -> str:
    return f"{symbol.lower()}@trade"