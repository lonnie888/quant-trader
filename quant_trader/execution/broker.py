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

            # Set leverage before placing market order
            try:
                self._post("leverage", {
                    "symbol": api_sym, "leverage": str(int(leverage)),
                })
                log.info("demo leverage set %s = %sx", api_sym, int(leverage))
            except Exception as e:
                log.warning("demo leverage set failed %s: %s", api_sym, e)

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

            # 4 个 Algo Order 挂单策略:
            # SL @ -3%  → closePosition=true (兜底，全平)
            # TP1 @ +6% → 平 50% 数量
            # TP2 @ +12% → 平 30% 数量
            # TP3 @ +18% → 平 20% 数量
            # 用 LOT_SIZE 取整防止精度问题

            def _lot_round(q: float) -> int:
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
                                if f["filterType"] == "LOT_SIZE":
                                    step = int(float(f["stepSize"]))
                                    return (int(q) // step) * step
                except Exception:
                    pass
                return int(q)

            def _post_algo(typ: str, trigger: float, qty_arg, suffix: str):
                params = {
                    "symbol": api_sym, "side": "SELL", "positionSide": "LONG",
                    "type": typ, "algoType": "CONDITIONAL",
                    "triggerPrice": str(round(trigger, 7)),
                    "workingType": "MARK_PRICE",
                }
                if qty_arg == "close":
                    params["closePosition"] = "true"
                else:
                    qty_rounded = _lot_round(qty_arg)
                    if qty_rounded <= 0:
                        return
                    params["quantity"] = str(qty_rounded)
                try:
                    self._post("algoOrder", params)
                    log.info("demo %s %s @ %s (qty=%s)", suffix, api_sym, params["triggerPrice"], qty_arg)
                except Exception as e:
                    log.warning("demo %s failed %s: %s (daemon will monitor)", suffix, api_sym, e)

            # SL/TP from strategy params (consistent with backtest)
            sl_pct = float(params.get("stop_loss_pct", 0.12))
            tp_pct = float(params.get("take_profit_pct", 0.20))

            # SL @ -sl_pct% 用 qty 100% (兼容性更好，不用 closePosition)
            _post_algo("STOP_MARKET", actual_price * (1 - sl_pct), qty, "SL")
            # TP @ +tp_pct% 用 qty 100%
            _post_algo("TAKE_PROFIT_MARKET", actual_price * (1 + tp_pct), qty, "TP")

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
                    # Calculate actual qty from position: margin = notional / leverage
                    # For paper ledger, we stored only entry_price and leverage
                    entry = float(e["entry_price"])
                    lev = float(e.get("leverage", 3.0))
                    # Use the position size from the paper ledger (qty = margin * lev / entry)
                    # Margin is fixed at 1000 USDT per position
                    margin = 1000.0
                    qty = int(margin * lev / entry)
                    from quant_trader.execution.broker import DemoBroker
                    # Round down to LOT_SIZE step
                    try:
                        import requests as _req
                        ei = _req.get(
                            f"{FAPI_BASE}/exchangeInfo",
                            params={"symbol": api_sym},
                            proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                            timeout=5,
                        ).json()
                        for sym_info in ei.get("symbols", []):
                            if sym_info["symbol"] == api_sym:
                                for f in sym_info.get("filters", []):
                                    if f["filterType"] == "LOT_SIZE":
                                        step = int(float(f["stepSize"]))
                                        qty = (qty // step) * step
                                        break
                    except Exception:
                        pass
                    if qty <= 0:
                        qty = 1
                    self._post("order", {
                        "symbol": api_sym, "side": "SELL", "type": "MARKET",
                        "quantity": str(qty), "positionSide": "LONG",
                    })
                    log.info("demo closed %s id=%s qty=%s", api_sym, position_id, qty)
                except Exception as e:
                    log.warning("demo close failed %s: %s", api_sym, e)
                break
        return self._paper.exit(
            position_id=position_id, exit_ts=exit_ts,
            exit_price=exit_price, exit_reason=exit_reason,
            log_path=log_path,
        )

    def get_positions(self) -> list[dict]:
        return get_open_positions()


def create_broker(settings, mode: str = "paper",
                  proxy: Optional[str] = None) -> BaseBroker:
    if mode == "demo":
        return DemoBroker(settings, proxy=proxy)
    return PaperBroker()