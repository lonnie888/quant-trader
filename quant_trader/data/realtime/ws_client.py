"""Fapi WebSocket client for Binance USDT-perp.

Provides async streaming of:
  - kline_1m / kline_15m streams
  - markPrice@1s streams

Features:
  - Auto-reconnect with exponential backoff
  - Multi-stream multiplexing on one connection
  - Callback dispatch by stream name
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import websockets

log = logging.getLogger(__name__)

WS_BASE = "wss://fstream.binance.com/ws"


class FapiWS:
    """Single multiplexed WebSocket connection to fapi."""

    def __init__(self, base_url: str = WS_BASE):
        self.base_url = base_url
        self.handlers: dict[str, list[Callable[[dict], Awaitable[None]]]] = {}
        self.subs: set[str] = set()
        self._ws = None
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
            await self._send_subscribe(new)

    async def _send_subscribe(self, streams: list[str]):
        msg = {"method": "SUBSCRIBE", "params": streams, "id": int(asyncio.get_event_loop().time() * 1000) % 100000}
        await self._ws.send(json.dumps(msg))
        log.info("subscribed: %s", streams)

    async def _reader(self):
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if self._stop:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "stream" in msg and "data" in msg:
                    stream = msg["stream"]
                    data = msg["data"]
                    for h in self.handlers.get(stream, []):
                        try:
                            await h(data)
                        except Exception as e:
                            log.exception("handler error on %s: %s", stream, e)
        except websockets.ConnectionClosed:
            log.warning("ws connection closed")
        except Exception as e:
            log.exception("ws reader error: %s", e)

    async def run(self, stop_event=None):
        """Main loop: connect, subscribe, read; reconnect on failure.
        Returns True if WS connected, False if all attempts failed (caller
        can fall back to REST polling)."""
        backoff = 1.0
        max_attempts = 3
        attempt = 0
        connected = False
        while not self._stop and attempt < max_attempts and not connected:
            if stop_event and stop_event.is_set():
                break
            attempt += 1
            try:
                log.info("connecting to %s (attempt %d/%d)", self.base_url, attempt, max_attempts)
                async with websockets.connect(self.base_url, ping_interval=20, ping_timeout=10, open_timeout=5) as ws:
                    self._ws = ws
                    if self.subs:
                        await self._send_subscribe(list(self.subs))
                    backoff = 1.0
                    attempt = 0
                    connected = True
                    self._reader_task = asyncio.create_task(self._reader())
                    await self._reader_task
            except (websockets.InvalidStatus, OSError, asyncio.TimeoutError) as e:
                log.warning("ws connection failed (attempt %d/%d): %s", attempt, max_attempts, e)
            except Exception as e:
                log.warning("ws disconnected: %s, retry in %.1fs", e, backoff)
            if self._stop or (stop_event and stop_event.is_set()):
                break
            if not connected and attempt < max_attempts:
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
        return connected

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


def stream_kline(symbol: str, interval: str) -> str:
    """Build kline stream name, e.g. 'btcusdt@kline_15m'."""
    return f"{symbol.lower()}@kline_{interval}"


def stream_mark(symbol: str) -> str:
    """Build mark price stream name."""
    return f"{symbol.lower()}@markPrice@1s"