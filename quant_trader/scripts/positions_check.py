"""positions_check.py — live status of all open paper positions.

Reads open events from reports/paper/positions.jsonl, fetches the latest
15m kline via the fapi public endpoint for each open symbol, and computes:
  - current PnL (raw and leveraged)
  - max favorable / adverse excursion since entry
  - whether stop-loss or take-profit has been hit on the most recent bar
  - hours remaining until the time-exit deadline

When an exit condition is detected (SL/TP/time/end-of-data), it
automatically writes a "closed" event back to the ledger via
paper_ledger.close_position() so the ledger stays in sync.

Writes a Markdown report to reports/paper/positions-{today}.md. Closed
positions are NOT evaluated here (use recap.py for closed PnL).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("positions_check")

DEFAULT_LOG = Path("reports/paper/positions.jsonl")
FAPI = "https://fapi.binance.com/fapi/v1/klines"


def _api_symbol(sym: str) -> str:
    return sym.split("/")[0].split(":")[0] + "USDT"


def _read_open(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    all_events: list[dict] = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                all_events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    # Build set of ids that have been closed/blocked
    closed_ids: set[int] = set()
    for ev in all_events:
        if ev.get("status") in ("closed", "blocked"):
            closed_ids.add(int(ev["id"]))
    # Only open events that haven't been closed
    open_pos = [ev for ev in all_events if ev.get("status") == "open" and int(ev["id"]) not in closed_ids]
    # Dedup: keep last open per symbol
    seen: dict[str, dict] = {}
    for ev in open_pos:
        seen[ev["symbol"]] = ev
    return list(seen.values())


def _fetch_klines_since(symbol: str, start_ms: int, end_ms: int) -> list[list]:
    out: list[list] = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(
            FAPI,
            params={"symbol": _api_symbol(symbol), "interval": "15m",
                    "startTime": cursor, "endTime": end_ms, "limit": 1000},
            timeout=15,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend([[float(x) for x in row[:6]] for row in batch])
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 15 * 60 * 1000
    return out


def _evaluate(open_ev: dict, klines: list[list]) -> dict:
    """Compute current PnL + max excursion + SL/TP status for an open position."""
    entry_ms = int(
        datetime.fromisoformat(open_ev["entry_ts"].replace("Z", "+00:00")).timestamp() * 1000
    )
    entry_price = float(open_ev["entry_price"])
    sl_price = float(open_ev["sl_price"])
    tp_price = open_ev.get("tp_price")
    tp_price = float(tp_price) if tp_price is not None else None
    lev = float(open_ev.get("leverage", 3.0))
    hold_bars = int(open_ev["params"].get("hold_bars", 24))
    exit_target_ms = entry_ms + hold_bars * 15 * 60 * 1000

    max_fav = 0.0
    max_adv = 0.0
    sl_hit_at = None
    tp_hit_at = None
    exit_at = None
    exit_reason = None
    exit_price = None

    for row in klines:
        ts, o, h, l, c, v = row
        if ts < entry_ms:
            continue
        fav = (h - entry_price) / entry_price
        adv = (l - entry_price) / entry_price
        if fav > max_fav:
            max_fav = fav
        if adv < max_adv:
            max_adv = adv
        if sl_hit_at is None and l <= sl_price:
            sl_hit_at = ts
            exit_at = ts
            exit_reason = "stop_loss"
            exit_price = sl_price
            break
        if tp_price is not None and tp_hit_at is None and h >= tp_price:
            tp_hit_at = ts
            exit_at = ts
            exit_reason = "take_profit"
            exit_price = tp_price
            break
        if ts >= exit_target_ms:
            exit_at = ts
            exit_reason = "time"
            exit_price = c
            break

    last_close = float(klines[-1][4]) if klines else entry_price
    cur_pnl = (last_close - entry_price) / entry_price if entry_price else 0.0
    cur_pnl_lev = cur_pnl * lev
    if exit_at is not None:
        closed_pnl = (exit_price - entry_price) / entry_price * lev
    else:
        closed_pnl = None
        # time remaining: from "now" in the most recent bar
        last_ts = int(klines[-1][0]) if klines else entry_ms
        remaining_ms = max(exit_target_ms - last_ts, 0)
        remaining_bars = int(remaining_ms // (15 * 60 * 1000))
    return {
        "id": open_ev["id"],
        "symbol": open_ev["symbol"],
        "strategy": open_ev["strategy"],
        "open_day": open_ev["open_day"],
        "entry_ts": open_ev["entry_ts"],
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "last_close": last_close,
        "max_favorable_pct": max_fav,
        "max_adverse_pct": max_adv,
        "current_pnl_pct": cur_pnl,
        "current_pnl_pct_lev": cur_pnl_lev,
        "closed_pnl_pct_lev": closed_pnl,
        "exit_reason": exit_reason,
        "exit_ts": exit_at,
        "exit_price": exit_price,
        "remaining_bars": remaining_bars if exit_at is None else 0,
        "leverage": lev,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--log", default=str(DEFAULT_LOG))
    p.add_argument("--out", default=None)
    p.add_argument("--dry-run", action="store_true",
                   help="detect exits but do NOT write close events to ledger")
    args = p.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        log.error("ledger %s does not exist", log_path)
        return
    open_evs = _read_open(log_path)
    log.info("found %d open positions", len(open_evs))
    if not open_evs:
        out_path = Path(args.out) if args.out else Path("reports/paper/positions-today.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("# Positions - no open positions\n", encoding="utf-8")
        return

    from quant_trader.execution.paper_ledger import close_position
    from quant_trader.execution.notifier import FeishuNotifier
    from quant_trader.execution.paper_ledger import get_all_positions

    feishu = FeishuNotifier()

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    rows: list[dict] = []
    for ev in open_evs:
        try:
            entry_ms = int(datetime.fromisoformat(ev["entry_ts"].replace("Z", "+00:00")).timestamp() * 1000)
        except Exception as e:
            log.warning("bad entry_ts on id=%d: %s", ev["id"], e)
            continue
        try:
            klines = _fetch_klines_since(ev["symbol"], entry_ms, now_ms)
        except Exception as e:
            log.warning("fetch failed %s: %s", ev["symbol"], e)
            continue
        if not klines:
            log.warning("no klines for %s since entry", ev["symbol"])
            continue
        r = _evaluate(ev, klines)
        rows.append(r)

        # Auto-close: write a close event to the ledger when exit is detected
        if r["exit_reason"] is not None and not args.dry_run:
            close_ts = datetime.fromisoformat(
                ev["entry_ts"].replace("Z", "+00:00")
            ).strftime("%Y-%m-%dT%H:%M:%S+00:00") if r["exit_ts"] else None
            # use the kline timestamp as exit time
            exit_iso = datetime.fromtimestamp(r["exit_ts"] / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S+00:00"
            ) if r["exit_ts"] else None
            close_position(
                position_id=ev["id"],
                exit_ts=exit_iso or close_ts,
                exit_price=r["exit_price"] or r["last_close"],
                exit_reason=r["exit_reason"],
                log_path=log_path,
            )
            # Notify Feishu on auto-close
            from quant_trader.execution.notifier import FeishuCardBuilder
            close_card = FeishuCardBuilder.make_position_close(
                symbol=r["symbol"], exit_reason=r["exit_reason"],
                entry_price=r["entry_price"], exit_price=r["exit_price"],
                pnl_pct_lev=r["closed_pnl_pct_lev"] or 0.0,
                max_fav_pct=r["max_favorable_pct"],
                max_adv_pct=r["max_adverse_pct"],
            )
            feishu.send_card(close_card)

    if not rows:
        log.error("no positions evaluated")
        return

    n = len(rows)
    wins = sum(1 for r in rows if (r["closed_pnl_pct_lev"] if r["closed_pnl_pct_lev"] is not None else r["current_pnl_pct_lev"]) > 0)
    total_unrealized = sum(r["current_pnl_pct_lev"] for r in rows if r["closed_pnl_pct_lev"] is None)

    # Read cumulative realized PnL from ledger (all closed events today)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    all_events = get_all_positions(log_path)
    realized_today = 0.0
    for e in all_events:
        if e.get("status") == "closed" and e.get("exit_ts"):
            try:
                d = datetime.fromisoformat(e["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                continue
            if d == today:
                realized_today += e.get("pnl_pct_lev", 0.0)

    out_path = Path(args.out) if args.out else Path(f"reports/paper/positions-{today}.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        f"# Positions - {today}",
        "",
        f"_Checked {n} open positions at {datetime.now(timezone.utc).isoformat(timespec='seconds')}_",
        "",
        f"**Aggregate**: unrealized={total_unrealized*100:+.2f}%  realized_today={realized_today*100:+.2f}%  profit={wins}/{n}",
        "",
        "| id | symbol | strategy | entry_price | last | pnl% (lev) | max_fav% | max_adv% | state | bars_left |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | :---: | ---: |",
    ]
    for r in sorted(rows, key=lambda x: -(x["closed_pnl_pct_lev"] if x["closed_pnl_pct_lev"] is not None else x["current_pnl_pct_lev"])):
        if r["closed_pnl_pct_lev"] is not None:
            pnl = r["closed_pnl_pct_lev"]
            state = f"closed:{r['exit_reason']}"
            bars_left = 0
        else:
            pnl = r["current_pnl_pct_lev"]
            state = "open"
            bars_left = r["remaining_bars"]
        lines.append("| %d | %s | %s | %.6f | %.6f | %+.2f | %+.2f | %+.2f | %s | %d |" % (
            r["id"], r["symbol"], r["strategy"], r["entry_price"], r["last_close"],
            pnl * 100, r["max_favorable_pct"] * 100, r["max_adverse_pct"] * 100,
            state, bars_left,
        ))

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %s", out_path)

    # Summary notification
    # Summary counts from ledger
    open_count = len(rows)
    closed_count = sum(1 for e in all_events if e.get("status") == "closed")
    profitable = sum(1 for r in rows if r["current_pnl_pct_lev"] > 0)

    # Summary notification (interactive card)
    from quant_trader.execution.notifier import FeishuCardBuilder
    summary_card = FeishuCardBuilder.make_positions_check(
        today=today,
        total_unrealized_pct=total_unrealized * 100,
        total_realized_pct=realized_today * 100,
        open_count=open_count,
        closed_count=closed_count,
        profitable=profitable,
        positions=rows,
    )
    feishu.send_card(summary_card)

    print(out_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()