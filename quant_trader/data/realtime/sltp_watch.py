"""Mark price monitor - checks SL/TP against current mark price for open positions.

Subscribes to <symbol>@markPrice@1s for every open position symbol.
On each tick, evaluates against sl_price / tp_price / hold_bars expiry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ...execution.paper_ledger import get_all_positions, close_position, _has_open

log = logging.getLogger(__name__)


class SLTPWatch:
    """On each mark tick, decide if open position should be closed."""

    def __init__(self, on_close=None):
        self.on_close = on_close
        self.subscribed: set[str] = set()
        self.open_positions: dict[int, dict] = {}

    def refresh_open(self):
        """Reload open positions from ledger."""
        positions_path = Path("reports/paper/positions.jsonl")
        all_events = get_all_positions(positions_path)
        # 1. Build open positions map
        pos = {}
        for e in all_events:
            if e.get("status") == "open":
                pos[int(e["id"])] = e
        # 2. Remove those that have been closed
        open_ids = set(pos.keys())
        # 3. Match the open dict
        self.open_positions = pos
        symbols = {p["symbol"] for p in pos.values()}
        return symbols

    def is_subscribed(self, symbol: str) -> bool:
        return symbol.upper() in self.subscribed

    def mark_subscribed(self, symbol: str):
        self.subscribed.add(symbol.upper())

    def on_mark(self, symbol: str, mark_price: float):
        # Always re-read open positions from ledger (avoids stale in-memory state).
        from quant_trader.execution.paper_ledger import get_all_positions
        all_events = get_all_positions()
        closed_ids = set()
        for e in all_events:
            if e.get("status") in ("closed", "blocked"):
                closed_ids.add(int(e["id"]))
        live_open = [
            e for e in all_events
            if e.get("status") == "open" and int(e["id"]) not in closed_ids
        ]
        for pos in live_open:
            if pos["symbol"].upper() != symbol.upper():
                continue
            pos_id = int(pos["id"])
            try:
                entry = float(pos["entry_price"])
                sl = float(pos["sl_price"])
                tp = pos.get("tp_price")
                tp = float(tp) if tp is not None else None
                lev = float(pos.get("leverage", 3.0))
                hold_bars = int(pos["params"].get("hold_bars", 24))
                sl_pct = float(pos["params"].get("stop_loss_pct", 0.0))
                tp_pct = float(pos["params"].get("take_profit_pct", 0.0))
            except (KeyError, TypeError, ValueError) as e:
                log.warning("malformed position id=%d: %s", pos_id, e)
                continue

            exit_reason = None
            exit_price = None
            if mark_price <= sl:
                exit_reason = "stop_loss"
                exit_price = sl
            elif tp is not None and mark_price >= tp:
                exit_reason = "take_profit"
                exit_price = tp
            else:
                # Check hold expiry
                entry_ts = pos.get("entry_ts", "")
                if entry_ts:
                    try:
                        ed = datetime.fromisoformat(entry_ts.replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        elapsed_bars = (now - ed).total_seconds() / (15 * 60)
                        if elapsed_bars >= hold_bars:
                            exit_reason = "time"
                            exit_price = mark_price
                    except Exception:
                        pass

            if exit_reason is None:
                continue

            exit_ts = datetime.now(timezone.utc).isoformat()
            closed = close_position(
                position_id=pos_id,
                exit_ts=exit_ts,
                exit_price=exit_price or mark_price,
                exit_reason=exit_reason,
            )
            if closed:
                log.info("closed id=%d %s @ %.6f reason=%s", pos_id, symbol, exit_price, exit_reason)
                if self.on_close is not None:
                    try:
                        self.on_close(closed)
                    except Exception as e:
                        log.exception("on_close error: %s", e)