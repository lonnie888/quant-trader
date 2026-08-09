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
        self._price_tracker: dict[int, dict] = {}  # pos_id -> {"min": price, "max": price}
        self._processed_ids: set = set()  # 已处理过的id，防止重复通知

    def on_mark(self, symbol: str, mark_price: float):
        # Always re-read open positions from ledger (avoids stale in-memory state).
        all_events = get_all_positions()
        closed_ids = set()
        for ev in all_events:
            if ev.get("status") in ("closed", "blocked"):
                closed_ids.add(int(ev["id"]))
        # 合并已处理的id和账本中的closed id
        all_processed = closed_ids | self._processed_ids
        # Clean up price tracker and processed ids
        for pid in list(self._price_tracker.keys()):
            if pid in all_processed:
                del self._price_tracker[pid]
        # 清理已处理的id（账本已经确认closed的可以移除）
        self._processed_ids -= closed_ids
        live_open = [
            ev for ev in all_events
            if ev.get("status") == "open" and int(ev["id"]) not in all_processed
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
            except (KeyError, TypeError, ValueError) as ex:
                log.warning("malformed position id=%d: %s", pos_id, ex)
                continue

            # Track max favorable / adverse price movement
            if pos_id not in self._price_tracker:
                self._price_tracker[pos_id] = {"min": mark_price, "max": mark_price}
            tracker = self._price_tracker[pos_id]
            if mark_price < tracker["min"]:
                tracker["min"] = mark_price
            if mark_price > tracker["max"]:
                tracker["max"] = mark_price

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

            # Compute max favorable/adverse from tracked prices
            max_fav = 0.0
            max_adv = 0.0
            if entry > 0 and pos_id in self._price_tracker:
                tr = self._price_tracker[pos_id]
                max_fav = (tr["max"] - entry) / entry
                max_adv = (tr["min"] - entry) / entry
                del self._price_tracker[pos_id]

            exit_ts = datetime.now(timezone.utc).isoformat()
            # Build closed dict for callback (actual close_position() is called by the callback via broker.exit())
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
                "max_fav_pct": max_fav,
                "max_adv_pct": max_adv,
            }
            log.info("closed id=%d %s @ %.6f reason=%s", pos_id, symbol, exit_price, exit_reason)
            self._processed_ids.add(pos_id)
            if self.on_close is not None:
                try:
                    self.on_close(closed)
                except Exception as ex:
                    log.exception("on_close error: %s", ex)