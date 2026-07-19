"""Broker interface — paper (JSONL) vs demo trading (Binance demo API)."""
from __future__ import annotations

import hashlib
import hmac
import logging
import time as _time
from pathlib import Path
from typing import Optional

import requests as _requests

from quant_trader.execution.paper_ledger import open_position as _paper_open
from quant_trader.execution.paper_ledger import get_open_positions, close_position

log = logging.getLogger(__name__)

FAPI_BASE = "https://demo-fapi.binance.com/fapi/v1"
FAPI_BASE_V2 = "https://demo-fapi.binance.com/fapi/v2"


def _sign(secret: str, params: dict) -> str:
    q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()


def _sign_and_send(method: str, path: str, api_key: str, secret: str,
                    params: dict, proxy: Optional[str] = None,
                    base_url: str = FAPI_BASE) -> dict:
    params["timestamp"] = int(_time.time() * 1000)
    params["recvWindow"] = 10000
    q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = _sign(secret, params)
    url = f"{base_url}/{path}?{q}&signature={sig}"
    proxies = {"http": proxy, "https": proxy} if proxy else None
    fn = _requests.get if method == "GET" else _requests.post
    r = fn(url, headers={"X-MBX-APIKEY": api_key}, proxies=proxies, timeout=10)
    r.raise_for_status()
    return r.json()


class BaseBroker:
    def enter(self, **kwargs):
        raise NotImplementedError
    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        raise NotImplementedError
    def get_positions(self) -> list[dict]:
        raise NotImplementedError


class PaperBroker(BaseBroker):
    def enter(self, **kwargs):
        return _paper_open(**kwargs)
    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        return close_position(position_id, exit_ts=exit_ts,
                              exit_price=exit_price, exit_reason=exit_reason)
    def get_positions(self) -> list[dict]:
        return get_open_positions()


class DemoBroker(BaseBroker):
    def __init__(self, settings, proxy: Optional[str] = None):
        cfg = settings.demo_trading
        self.api_key = cfg.api_key
        self.secret = cfg.api_secret
        self.proxy = proxy
        self._paper = PaperBroker()
        self.leverage = int(getattr(settings.backtest, "leverage", 3))

    def _api(method: str, path: str, params: dict) -> dict:
        return _sign_and_send(method, path, self.api_key, self.secret, params, self.proxy)

    def _post(self, path: str, params: dict) -> dict:
        return _sign_and_send("POST", path, self.api_key, self.secret, params, self.proxy)

    def _get(self, path: str, params: dict, base_url: str = FAPI_BASE) -> dict:
        return _sign_and_send("GET", path, self.api_key, self.secret, params, self.proxy, base_url)

    def enter(self, *, symbol: str, strategy: str, params: dict,
              entry_ts: str, entry_price: float, leverage: float,
              open_day: Optional[str] = None, log_path: Path,
              risk_check: Optional[dict] = None):
        api_sym = symbol.split("/")[0].split(":")[0] + "USDT"

        try:
            # Get available balance
            acct = self._get("account", {}, base_url=FAPI_BASE_V2)
            free = float(acct.get("availableBalance", "0") or 0)
            # Fixed 1000 USDT margin per position; qty = margin * leverage / entry_price
            margin = 1000.0
            if free < margin:
                log.warning("跳过 %s: 余额不足(%.2f < 1000 USDT)", api_sym, free)
                return self._paper.enter(
                    symbol=symbol, strategy=strategy, params=params,
                    entry_ts=entry_ts, entry_price=entry_price,
                    leverage=leverage, open_day=open_day,
                    log_path=log_path, risk_check=risk_check,
                )
            raw_qty = int(margin * leverage / entry_price)
            min_qty = max(int(5.0 / entry_price), 1)
            qty = max(raw_qty, min_qty)

            # Fetch exchange info to respect LOT_SIZE/MARKET_LOT_SIZE
            try:
                ei = _requests.get(
                    f"{FAPI_BASE}/exchangeInfo",
                    params={"symbol": api_sym},
                    proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                    timeout=5,
                ).json()
                for sym in ei.get("symbols", []):
                    if sym["symbol"] == api_sym:
                        for f in sym.get("filters", []):
                            if f["filterType"] == "MARKET_LOT_SIZE":
                                max_qty = int(float(f["maxQty"]))
                                qty = min(qty, max_qty)
                            if f["filterType"] == "LOT_SIZE":
                                step = int(float(f["stepSize"]))
                                qty = (qty // step) * step  # round down to step
            except Exception:
                pass  # fallback: use raw qty

            if qty <= 0:
                log.warning("跳过 %s: 保证金不足(%.2f USDT)", api_sym, free)
                return self._paper.enter(
                    symbol=symbol, strategy=strategy, params=params,
                    entry_ts=entry_ts, entry_price=entry_price,
                    leverage=leverage, open_day=open_day,
                    log_path=log_path, risk_check=risk_check,
                )

            # Market buy
            log.info("demo buy %s qty=%s", api_sym, qty)
            order = self._post("order", {
                "symbol": api_sym, "side": "BUY", "type": "MARKET",
                "quantity": str(qty), "positionSide": "LONG",
            })
            oid = order.get("orderId", "?")
            _time.sleep(0.5)
            fo = self._get("order", {"symbol": api_sym, "orderId": str(oid)})
            filled = float(fo.get("executedQty", 0) or 0)
            cum = float(fo.get("cumQuote", 0) or 0)
            actual_price = cum / filled if filled > 0 else entry_price
            log.info("demo filled %s qty=%s price=%s(%s) id=%s", api_sym, qty, actual_price, fo.get("avgPrice","?"), oid)

            # SL/TP via Algo Order (CONDITIONAL) — requires algoType, triggerPrice, workingType
            sl_p = round(actual_price * 0.90, 8)
            try:
                self._post("algoOrder", {
                    "symbol": api_sym, "side": "SELL", "positionSide": "LONG",
                    "type": "STOP_MARKET", "algoType": "CONDITIONAL",
                    "quantity": str(qty), "triggerPrice": str(sl_p),
                    "workingType": "MARK_PRICE",
                })
                log.info("demo SL %s @ %s", api_sym, sl_p)
            except Exception as e:
                log.warning("demo SL failed %s: %s (daemon will monitor)", api_sym, e)

            tp_p = round(actual_price * 1.30, 8)
            try:
                self._post("algoOrder", {
                    "symbol": api_sym, "side": "SELL", "positionSide": "LONG",
                    "type": "TAKE_PROFIT_MARKET", "algoType": "CONDITIONAL",
                    "quantity": str(qty), "triggerPrice": str(tp_p),
                    "workingType": "MARK_PRICE",
                })
                log.info("demo TP %s @ %s", api_sym, tp_p)
            except Exception as e:
                log.warning("demo TP failed %s: %s (daemon will monitor)", api_sym, e)

        except Exception as e:
            log.warning("demo failed %s: %s, fallback paper", api_sym, e)
            return self._paper.enter(
                symbol=symbol, strategy=strategy, params=params,
                entry_ts=entry_ts, entry_price=entry_price,
                leverage=leverage, open_day=open_day,
                log_path=log_path, risk_check=risk_check,
            )

        ev = self._paper.enter(
            symbol=symbol, strategy=strategy, params=params,
            entry_ts=entry_ts, entry_price=actual_price,
            leverage=leverage, open_day=open_day,
            log_path=log_path, risk_check=risk_check,
        )
        return ev

    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        from quant_trader.execution.paper_ledger import get_all_positions
        events = get_all_positions(log_path)
        for e in events:
            if e.get("id") == position_id and e.get("status") == "open":
                sym = e["symbol"]
                api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                try:
                    self._post("order", {
                        "symbol": api_sym, "side": "SELL", "type": "MARKET",
                        "quantity": "1", "positionSide": "LONG",
                    })
                    log.info("demo closed %s id=%s", api_sym, position_id)
                except Exception as e:
                    log.warning("demo close failed %s: %s", api_sym, e)
                break
        return self._paper.exit(
            position_id=position_id, exit_ts=exit_ts,
            exit_price=exit_price, exit_reason=exit_reason,
        )

    def get_positions(self) -> list[dict]:
        return get_open_positions()


def create_broker(settings, mode: str = "paper",
                  proxy: Optional[str] = None) -> BaseBroker:
    if mode == "demo":
        return DemoBroker(settings, proxy=proxy)
    return PaperBroker()