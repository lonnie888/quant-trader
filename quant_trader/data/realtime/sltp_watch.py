"""Mark price monitor - checks SL/TP against current mark price for open positions.

On each tick, evaluates against sl_price / tp_price / hold_bars expiry.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...execution.paper_ledger import get_all_positions

log = logging.getLogger(__name__)


class SLTPWatch:
    """On each mark tick, decide if open position should be closed."""

    def __init__(self, on_close=None):
        self.on_close = on_close

    def on_mark(self, symbol: str, mark_price: float):
        # Always re-read open positions from ledger (avoids stale in-memory state).
        all_events = get_all_positions()
        closed_ids = set()
        for ev in all_events:
            if ev.get("status") in ("closed", "blocked"):
                closed_ids.add(int(ev["id"]))
        live_open = [
            ev for ev in all_events
            if ev.get("status") == "open" and int(ev["id"]) not in closed_ids
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
                hold_bars = int(pos["params"].get("hold_bars", 24))
            except (KeyError, TypeError, ValueError) as ev:
                log.warning("malformed position id=%d: %s", pos_id, ex)
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
            # Build closed dict for callback (actual close_position() is called by the callback via broker.exit())
            import json
            closed = {
                "id": pos_id,
                "status": "closed",
                "symbol": pos.get("symbol", symbol),
                "strategy": pos.get("strategy", ""),
                "params": pos.get("params", {}),
                "open_day": pos.get("open_day", ""),
                "entry_ts": pos.get("entry_ts", ""),
                "entry_price": entry,
                "sl_price": pos.get("sl_price", 0),
                "tp_price": pos.get("tp_price"),
                "leverage": pos.get("leverage", 3.0),
                "exit_ts": exit_ts,
                "exit_price": exit_price or mark_price,
                "exit_reason": exit_reason,
            }
            log.info("closed id=%d %s @ %.6f reason=%s", pos_id, symbol, exit_price, exit_reason)
            if self.on_close is not None:
                try:
                    self.on_close(closed)
                except Exception as ex:
                    log.exception("on_close error: %s", ex)