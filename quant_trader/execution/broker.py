"""Broker interface — paper (JSONL) vs demo trading (Binance demo API)."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from quant_trader.execution.paper_ledger import open_position as _paper_open
from quant_trader.execution.paper_ledger import get_open_positions, close_position

log = logging.getLogger(__name__)

FIXED_MARGIN = 1000.0  # USDT per position


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
    """Binance USDⓈ-M Futures demo trading broker (demo-fapi.binance.com)."""

    def __init__(self, settings, proxy: Optional[str] = None):
        import ccxt
        cfg = settings.demo_trading
        self.exchange = ccxt.binance({
            "apiKey": cfg.api_key,
            "secret": cfg.api_secret,
            "options": {"defaultType": "future"},
        })
        self.exchange.enable_demo_trading(True)
        if proxy:
            self.exchange.proxies = {"http": proxy, "https": proxy}
        self._paper = PaperBroker()
        self.leverage = int(getattr(settings.backtest, "leverage", 3))

    def _set_leverage(self, symbol_ccxt: str):
        try:
            self.exchange.set_leverage(self.leverage, symbol_ccxt)
        except Exception as e:
            log.debug("set_leverage %s: %s", symbol_ccxt, e)

    def enter(self, *, symbol: str, strategy: str, params: dict,
              entry_ts: str, entry_price: float, leverage: float,
              open_day: Optional[str] = None, log_path: Path,
              risk_check: Optional[dict] = None):
        sym_ccxt = symbol.split("/")[0].split(":")[0] + "/USDT"
        api_sym = symbol.split("/")[0].split(":")[0] + "USDT"

        # Fixed 1000 USDT margin per position
        raw_qty = FIXED_MARGIN * self.leverage / entry_price

        try:
            qty = float(self.exchange.amount_to_precision(sym_ccxt, raw_qty))
        except Exception:
            qty = max(round(raw_qty), 1)

        # Minimum notional = 5 USDT
        min_qty = max(1, int(5.0 / entry_price))
        qty = max(qty, min_qty)

        # Clamp to available balance
        try:
            bal = self.exchange.fetch_balance()
            free = float(bal.get("USDT", {}).get("free", 0))
            max_q = max(1, int(free * self.leverage / entry_price))
            qty = min(qty, max_q)
        except Exception:
            pass
        qty = max(qty, min_qty)

        try:
            self._set_leverage(sym_ccxt)
            order = self.exchange.create_market_buy_order(
                sym_ccxt, qty,
                params={"positionSide": "LONG"},
            )
            filled = float(order.get("filled", 0))
            cost = float(order.get("cost", 0))
            if cost > 0 and filled > 0:
                actual_price = cost / filled
            else:
                actual_price = float(order.get("price", entry_price) or entry_price)
            log.info("demo order filled %s qty=%s price=%s id=%s",
                     api_sym, qty, actual_price, order.get("id", "?"))
        except Exception as e:
            log.warning("demo order failed %s: %s, falling back to paper", api_sym, e)
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
                sym_ccxt = sym.split("/")[0].split(":")[0] + "/USDT"
                api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                try:
                    self.exchange.create_market_sell_order(
                        sym_ccxt, 1,
                        params={"positionSide": "LONG", "reduceOnly": True},
                    )
                    log.info("demo order closed %s id=%s", api_sym, position_id)
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