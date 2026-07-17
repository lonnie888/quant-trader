"""Broker interface — paper (JSONL) vs demo trading (Binance demo API).

Usage:
  from quant_trader.execution.broker import create_broker
  broker = create_broker(settings)
  broker.enter(symbol=sym, entry_price=price, ...)
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from quant_trader.execution.paper_ledger import open_position as _paper_open
from quant_trader.execution.paper_ledger import get_all_positions, get_open_positions, close_position

log = logging.getLogger(__name__)

LEVERAGE = 3


class BaseBroker:
    """Abstract broker interface."""

    def enter(self, *, symbol: str, strategy: str, params: dict,
              entry_ts: str, entry_price: float, leverage: float,
              open_day: Optional[str] = None, log_path: Path,
              risk_check: Optional[dict] = None):
        raise NotImplementedError

    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        raise NotImplementedError

    def get_positions(self) -> list[dict]:
        raise NotImplementedError


class PaperBroker(BaseBroker):
    """Existing JSONL-based paper trading broker."""

    def enter(self, **kwargs):
        return _paper_open(**kwargs)

    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        return close_position(position_id, exit_ts=exit_ts,
                              exit_price=exit_price, exit_reason=exit_reason)

    def get_positions(self) -> list[dict]:
        return get_open_positions()


class DemoBroker(BaseBroker):
    """Binance USDⓈ-M Futures demo trading broker (demo-fapi.binance.com).
    
    Uses ccxt binance with enable_demo_trading().
    Falls back to paper ledger for positions missing on exchange.
    """

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
        """Set leverage on first use per symbol."""
        try:
            self.exchange.set_leverage(self.leverage, symbol_ccxt)
        except Exception as e:
            log.debug("set_leverage %s: %s", symbol_ccxt, e)

    def enter(self, *, symbol: str, strategy: str, params: dict,
              entry_ts: str, entry_price: float, leverage: float,
              open_day: Optional[str] = None, log_path: Path,
              risk_check: Optional[dict] = None):
        # 1. Open on exchange via market order
        sym_ccxt = symbol.split("/")[0].split(":")[0] + "/USDT"
        api_sym = symbol.split("/")[0].split(":")[0] + "USDT"
        # Calculate quantity from risk_check (max_position_pct of capital)
        capital = float(risk_check.get("initial_capital", 10000)) if risk_check else 10000
        pos_pct = float(risk_check.get("max_position_pct", 0.10)) if risk_check else 0.10
        notional = capital * pos_pct * leverage
        raw_qty = notional / entry_price
        try:
            qty = float(self.exchange.amount_to_precision(sym_ccxt, raw_qty))
        except Exception:
            qty = max(round(raw_qty), 1)  # fallback: integer

        try:
            self._set_leverage(sym_ccxt)
            order = self.exchange.create_market_buy_order(sym_ccxt, qty)
            log.info("demo order filled: %s qty=%s price=%s", api_sym, qty,
                     order.get("price", "?"))
            # Use fill price from exchange
            actual_price = float(order.get("price", entry_price))
        except Exception as e:
            log.warning("demo order failed %s: %s, falling back to paper", api_sym, e)
            return self._paper.enter(
                symbol=symbol, strategy=strategy, params=params,
                entry_ts=entry_ts, entry_price=entry_price,
                leverage=leverage, open_day=open_day,
                log_path=log_path, risk_check=risk_check,
            )

        # 2. Record in paper ledger for unified tracking
        ev = self._paper.enter(
            symbol=symbol, strategy=strategy, params=params,
            entry_ts=entry_ts, entry_price=actual_price,
            leverage=leverage, open_day=open_day,
            log_path=log_path, risk_check=risk_check,
        )
        return ev

    def exit(self, *, position_id: int, exit_ts: str,
             exit_price: float, exit_reason: str, log_path: Path):
        # 1. Close on exchange
        # TODO: read the position's symbol from paper ledger, then market sell
        # 2. Record in paper ledger
        return self._paper.exit(
            position_id=position_id, exit_ts=exit_ts,
            exit_price=exit_price, exit_reason=exit_reason,
        )

    def get_positions(self) -> list[dict]:
        return get_open_positions()


def create_broker(settings, mode: str = "paper",
                  proxy: Optional[str] = None) -> BaseBroker:
    """Factory. mode='paper' or mode='demo'."""
    if mode == "demo":
        return DemoBroker(settings, proxy=proxy)
    return PaperBroker()