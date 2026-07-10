"""Paper-trade ledger: append-only log of open/close events.

Each event is one line of JSON in `reports/paper/positions.jsonl`. A position
goes through three states:

  - "open":    signal fired; the position is held in paper mode
  - "closed":  position closed (SL hit, TP hit, hold expired, or manual)
  - "blocked": signal was rejected by the risk manager (no position opened)

`recap.py` reads closed events to compute realized PnL.
`positions_check.py` reads open events to compute live unrealized PnL and
detect SL/TP hits on the latest bar.

Fields:
  - id:          monotonically increasing int (assigned at open time)
  - status:      "open" | "closed" | "blocked"
  - symbol:      "XXX/USDT:USDT"
  - strategy:    strategy class name
  - params:      dict of strategy params (incl. hold_bars, stop_loss_pct, take_profit_pct)
  - open_day:    YYYY-MM-DD the signal was generated
  - entry_ts:    ISO timestamp of the entry bar
  - entry_price: signal bar close
  - sl_price:    absolute stop-loss price (entry * (1 - stop_loss_pct))
  - tp_price:    absolute take-profit price (entry * (1 + take_profit_pct)), or null
  - leverage:    int multiplier
  - exit_ts:     ISO timestamp at close (only when status=closed)
  - exit_price:  close price (only when status=closed)
  - exit_reason: "stop_loss" | "take_profit" | "time" | "manual"
  - pnl_pct:     raw return (not leveraged), only when closed
  - pnl_pct_lev: leveraged return, only when closed
  - block_reason: short string when status=blocked (e.g. "max_concurrent")
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_LOG = Path("reports/paper/positions.jsonl")


@dataclass
class PositionEvent:
    id: int
    status: str
    symbol: str
    strategy: str
    params: dict
    open_day: str
    entry_ts: str
    entry_price: float
    sl_price: float
    tp_price: Optional[float]
    leverage: float = 3.0
    exit_ts: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl_pct: Optional[float] = None
    pnl_pct_lev: Optional[float] = None
    block_reason: Optional[str] = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@contextmanager
def _file_lock(path: Path):
    """Acquire an exclusive flock on the log file for safe concurrent read+write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _next_id(events: list[dict]) -> int:
    return (max((e["id"] for e in events), default=0) + 1)


def _has_open(events: list[dict], symbol: str) -> bool:
    closed_ids: set[int] = set()
    for e in events:
        if e.get("status") in ("closed", "blocked"):
            closed_ids.add(int(e["id"]))
    for e in events:
        if e["symbol"] == symbol and e["status"] == "open" and int(e["id"]) not in closed_ids:
            return True
    return False


def _today_realized_pnl(events: list[dict], today: str) -> float:
    """Sum leveraged PnL of all events that closed today (UTC date)."""
    total = 0.0
    for e in events:
        if e["status"] != "closed":
            continue
        if not e.get("exit_ts"):
            continue
        # exit_ts is ISO; compare its UTC date to today
        try:
            d = datetime.fromisoformat(e["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            continue
        if d != today:
            continue
        pnl = e.get("pnl_pct_lev") or 0.0
        total += pnl
    return total


def evaluate_risk(
    events: list[dict],
    *,
    initial_capital: float,
    max_position_pct: float,
    max_total_exposure: float,
    daily_loss_limit: float,
    max_concurrent: int,
) -> tuple[bool, str]:
    """Decide whether a new entry is allowed.

    Returns (allowed, reason). reason is "" when allowed, otherwise a short
    tag such as "max_concurrent", "max_total_exposure", "daily_loss_limit".
    """
    # Only count open events without matching closed/blocked
    closed_ids: set[int] = set()
    for e in events:
        if e.get("status") in ("closed", "blocked"):
            closed_ids.add(int(e["id"]))
    open_pos = [e for e in events if e["status"] == "open" and int(e["id"]) not in closed_ids]
    if len(open_pos) >= max_concurrent:
        return False, "max_concurrent"

    # estimate current exposure as sum of |entry_price * qty_pct| where qty_pct
    # is bounded by max_position_pct per slot. This is conservative (treats
    # each open position as fully sized).
    used_pct = min(len(open_pos) * max_position_pct, max_total_exposure)
    if used_pct + max_position_pct > max_total_exposure + 1e-9:
        return False, "max_total_exposure"

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    realized = _today_realized_pnl(events, today)
    if realized <= -abs(daily_loss_limit):
        return False, "daily_loss_limit"

    return True, ""


def open_position(
    *,
    symbol: str,
    strategy: str,
    params: dict,
    entry_ts: str,
    entry_price: float,
    leverage: float,
    open_day: Optional[str] = None,
    log_path: Path = DEFAULT_LOG,
    risk_check: Optional[dict] = None,
) -> Optional[PositionEvent]:
    """Append an "open" event. Skips if the symbol already has an open position
    or if `risk_check` denies the trade.

    `risk_check` (optional dict) must include: initial_capital, max_position_pct,
    max_total_exposure, daily_loss_limit, max_concurrent. When provided and the
    check fails, a "blocked" event is appended (instead of an "open" event) so
    the audit trail is preserved.

    Returns the new event, or None if skipped.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(log_path):
        events = _read_log(log_path)
        if _has_open(events, symbol):
            log.info("skip open: %s already has an open position", symbol)
            return None

        if risk_check is not None:
            allowed, reason = evaluate_risk(events, **risk_check)
            if not allowed:
                ev = PositionEvent(
                    id=_next_id(events),
                    status="blocked",
                    symbol=symbol,
                    strategy=strategy,
                    params=dict(params),
                    open_day=open_day or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
                    entry_ts=entry_ts,
                    entry_price=entry_price,
                    sl_price=entry_price * (1 - float(params.get("stop_loss_pct", 0.0))),
                    tp_price=(entry_price * (1 + float(params.get("take_profit_pct", 0.0)))
                              if float(params.get("take_profit_pct", 0.0)) > 0 else None),
                    leverage=leverage,
                    block_reason=reason,
                )
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(ev.to_json() + "\n")
                log.info("blocked %s id=%d reason=%s", symbol, ev.id, reason)
                return ev

        sl = float(params.get("stop_loss_pct", 0.0))
        tp = float(params.get("take_profit_pct", 0.0))
        ev = PositionEvent(
            id=_next_id(events),
            status="open",
            symbol=symbol,
            strategy=strategy,
            params=dict(params),
            open_day=open_day or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            entry_ts=entry_ts,
            entry_price=entry_price,
            sl_price=entry_price * (1 - sl) if sl > 0 else entry_price,
            tp_price=entry_price * (1 + tp) if tp > 0 else None,
            leverage=leverage,
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(ev.to_json() + "\n")
        log.info("opened %s @ %s id=%d sl=%s tp=%s", symbol, entry_price, ev.id, ev.sl_price, ev.tp_price)
    return ev


def get_open_positions(log_path: Path = DEFAULT_LOG) -> list[dict]:
    """Return positions with status='open' that have no matching closed/blocked event."""
    events = _read_log(log_path)
    closed_ids: set[int] = set()
    for e in events:
        if e.get("status") in ("closed", "blocked"):
            closed_ids.add(int(e["id"]))
    return [e for e in events if e["status"] == "open" and int(e["id"]) not in closed_ids]


def get_all_positions(log_path: Path = DEFAULT_LOG) -> list[dict]:
    return _read_log(log_path)


def close_position(
    position_id: int,
    *,
    exit_ts: str,
    exit_price: float,
    exit_reason: str,
    log_path: Path = DEFAULT_LOG,
) -> Optional[dict]:
    """Append a "close" event paired with the open event of the same id.

    PnL is computed at close time and frozen.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(log_path):
        events = _read_log(log_path)
        open_ev = next((e for e in events if e["id"] == position_id and e["status"] == "open"), None)
        if open_ev is None:
            log.warning("close: no open event with id=%d", position_id)
            return None
        entry = float(open_ev["entry_price"])
        lev = float(open_ev.get("leverage", 3.0))
        pnl_pct = (exit_price - entry) / entry if entry > 0 else 0.0
        pnl_lev = pnl_pct * lev
        close_ev = PositionEvent(
            id=position_id,
            status="closed",
            symbol=open_ev["symbol"],
            strategy=open_ev["strategy"],
            params=open_ev["params"],
            open_day=open_ev["open_day"],
            entry_ts=open_ev["entry_ts"],
            entry_price=entry,
            sl_price=open_ev["sl_price"],
            tp_price=open_ev.get("tp_price"),
            leverage=lev,
            exit_ts=exit_ts,
            exit_price=exit_price,
            exit_reason=exit_reason,
            pnl_pct=pnl_pct,
            pnl_pct_lev=pnl_lev,
        )
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(close_ev.to_json() + "\n")
        log.info("closed %s id=%d @ %s reason=%s pnl=%+.2f%%",
                 open_ev["symbol"], position_id, exit_price, exit_reason, pnl_lev * 100)
    return asdict(close_ev)