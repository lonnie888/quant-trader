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
from urllib.parse import quote

import aiohttp

log = logging.getLogger(__name__)

WS_BASE = "wss://fstream.binance.com/market"
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

    async def unsubscribe(self, streams: list[str]):
        """Unsubscribe streams (removes from connection)."""
        rem = [s for s in streams if s in self.subs]
        if not rem:
            return
        self.subs.difference_update(rem)
        if self._ws is not None:
            await self._ws.send_json({
                "method": "UNSUBSCRIBE",
                "params": rem,
                "id": int(asyncio.get_event_loop().time() * 1000) % 100000,
            })
            log.info("unsubscribed: %s", rem)

    async def _reader(self):
        assert self._ws is not None
        log.info("[reader] starting")
        try:
            async for msg in self._ws:
                if self._stop:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    # 处理 Binance 的 TEXT ping 消息
                    if "ping" in data:
                        ts = data["ping"]
                        log.info("[reader] ping=%s", ts)
                        await self._ws.send_json({"pong": ts})
                        continue
                    # stream 模式：消息为 {"stream":"...","data":{...}}
                    stream = data.get("stream", "")
                    payload = data.get("data", data)
                    # 用 symbol 匹配 handler（stream 名如 "tutusdt@kline_15m"，取前半部分）
                    sym = payload.get("s", stream.split("@")[0] if stream else "").upper()
                    if not sym:
                        log.info("[reader] raw msg: %s", str(data)[:200])
                    for h in self.handlers.get(sym, []):
                        try:
                            await h(payload)
                        except Exception as e:
                            log.exception("handler error on %s: %s", stream, e)
                elif msg.type == aiohttp.WSMsgType.CLOSED:
                    log.warning("ws closed")
                    break
        except Exception as e:
            log.exception("ws reader error: %s", e)
        log.info("[reader] ended")

    async def run(self, stop_event=None):
        """Main loop: connect, subscribe, read; reconnect on failure.

        使用 stream 模式 (wss://fstream.binance.com/market/stream?streams=...)，
        实测 /market/stream 才能收到 K 线数据，/ws 模式收不到。
        """
        self._session = aiohttp.ClientSession()
        try:
            max_attempts = 3
            backoff = 1.0
            connected = False
            while not (self._stop or (stop_event and stop_event.is_set())):
                try:
                    streams = "/".join(sorted(self.subs))
                    # 中文 symbol (如 龙虾) 需 URL 编码, 保留 @ _ / 分隔符
                    streams_path = quote(streams, safe='@/_')
                    url = f"{WS_BASE}/stream?streams={streams_path}" if streams else WS_BASE
                    log.info("connecting to %s (proxy=%s)", url, self.proxy)
                    kwargs = {"timeout": aiohttp.ClientWSTimeout(ws_close=8.0), "heartbeat": 20.0}
                    if self.proxy:
                        kwargs["proxy"] = self.proxy
                    ws = await self._session.ws_connect(url, **kwargs)
                    self._ws = ws
                    connected = True
                    self._reader_task = asyncio.create_task(self._reader())
                    await self._reader_task
                    break
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning("ws connect failed: %s", e)
                if not connected:
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