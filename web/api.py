"""REST API routes for the Quant Trader Web Dashboard."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

api_bp = Blueprint("api", __name__)

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "reports" / "paper" / "positions.jsonl"
RECAP_DIR = ROOT / "reports" / "paper"
FAPI = "https://fapi.binance.com/fapi/v1/klines"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_ledger() -> list[dict]:
    """Read all events from the append-only JSONL ledger."""
    if not LEDGER.exists():
        return []
    events: list[dict] = []
    with open(LEDGER, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def _strip_symbol(sym: str) -> str:
    """Strip 'TLM/USDT:USDT' -> 'TLM'."""
    return sym.split("/")[0].split(":")[0]


def _api_symbol(sym: str) -> str:
    """'TLM/USDT:USDT' -> 'TLMUSDT' for Binance fapi."""
    return _strip_symbol(sym) + "USDT"


def _parse_ts(ts: str) -> int:
    """ISO timestamp to epoch ms."""
    return int(
        datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() * 1000
    )


def _current_open_positions(events: list[dict]) -> list[dict]:
    """Return currently open positions (open event with no matching close)."""
    closed_ids: set[int] = set()
    for ev in events:
        if ev.get("status") in ("closed", "blocked"):
            closed_ids.add(int(ev["id"]))
    open_pos = [
        ev for ev in events
        if ev.get("status") == "open" and int(ev["id"]) not in closed_ids
    ]
    # Dedup: keep last per symbol
    seen: dict[str, dict] = {}
    for ev in open_pos:
        seen[ev["symbol"]] = ev
    return list(seen.values())


def _build_positions_data(open_evs: list[dict]) -> list[dict]:
    """Fetch current prices and compute PnL for open positions."""
    out: list[dict] = []
    for ev in open_evs:
        sym = ev["symbol"]
        entry_price = float(ev["entry_price"])
        sl_price = float(ev["sl_price"])
        tp_price = ev.get("tp_price")
        tp_price = float(tp_price) if tp_price is not None else None
        lev = float(ev.get("leverage", 3.0))
        hold_bars = int(ev["params"].get("hold_bars", 24))
        entry_ms = _parse_ts(ev["entry_ts"])
        exit_target_ms = entry_ms + hold_bars * 15 * 60 * 1000

        display_sym = _strip_symbol(sym)
        last_price = entry_price
        bars_held = 0
        remaining_bars = hold_bars
        max_fav = 0.0
        max_adv = 0.0

        try:
            r = requests.get(
                FAPI,
                params={
                    "symbol": _api_symbol(sym),
                    "interval": "15m",
                    "limit": 100,
                },
                timeout=10,
            )
            r.raise_for_status()
            klines = r.json()
        except Exception as e:
            log.warning("fetch failed %s: %s", sym, e)
            klines = []

        if klines:
            # Get last close price
            last_price = float(klines[-1][4])
            # Compute max favorable/adverse excursion and bars held
            now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
            for row in klines:
                ts, o, h, l, c, v = [float(x) for x in row[:6]]
                if ts < entry_ms:
                    continue
                bars_held += 1
                fav = (h - entry_price) / entry_price
                adv = (l - entry_price) / entry_price
                if fav > max_fav:
                    max_fav = fav
                if adv < max_adv:
                    max_adv = adv
            remaining_bars = max(0, int((exit_target_ms - now_ms) // (15 * 60 * 1000)))
            # If entry hasn't started yet, remaining_bars is hold_bars
            if remaining_bars > hold_bars:
                remaining_bars = hold_bars

        pnl_pct = (last_price - entry_price) / entry_price if entry_price else 0.0
        pnl_pct_lev = pnl_pct * lev * 100  # convert to percentage

        out.append({
            "id": int(ev["id"]),
            "symbol": display_sym,
            "entry_price": entry_price,
            "last_price": last_price,
            "pnl_pct_lev": round(pnl_pct_lev, 2),
            "sl_price": sl_price,
            "remaining_bars": remaining_bars,
            "entry_ts": ev["entry_ts"],
            "bars_held": bars_held,
            "max_fav": round(max_fav * 100, 2),
            "max_adv": round(max_adv * 100, 2),
            "leverage": lev,
        })
    return out


def _compute_summary(open_evs: list[dict], closed_events: list[dict]) -> dict:
    """Compute aggregate statistics."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Unrealized PnL: sum of current PnL for open positions
    positions = _build_positions_data(open_evs)
    unrealized_pnl = sum(p["pnl_pct_lev"] for p in positions) if positions else 0.0

    # Realized PnL: from closed events today
    realized_pnl = 0.0
    wins = 0
    total_trades = 0
    for ev in closed_events:
        if ev.get("status") == "closed" and ev.get("exit_ts"):
            try:
                d = datetime.fromisoformat(ev["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                continue
            if d == today:
                pnl = ev.get("pnl_pct_lev", 0.0) or 0.0
                realized_pnl += pnl
                total_trades += 1
                if pnl > 0:
                    wins += 1

    # All-time stats from all closed events (for win rate)
    all_closed = [ev for ev in closed_events if ev.get("status") == "closed"]
    for ev in all_closed:
        pnl = ev.get("pnl_pct_lev", 0.0) or 0.0
        if ev.get("exit_ts"):
            # Count unique trades (not already counted in today's loop)
            try:
                d = datetime.fromisoformat(ev["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
            except Exception:
                continue
            if d != today:
                total_trades += 1
                if pnl > 0:
                    wins += 1

    # All-time realized PnL (sum of all closed trades)
    total_realized = 0.0
    for ev in all_closed:
        pnl = ev.get("pnl_pct_lev", 0.0) or 0.0
        total_realized += pnl

    win_rate = round(wins / total_trades * 100, 1) if total_trades > 0 else 0.0

    # Daily PnL series from closed events grouped by day
    daily_map: dict[str, dict] = {}
    for ev in closed_events:
        if ev.get("status") != "closed" or not ev.get("exit_ts"):
            continue
        try:
            d = datetime.fromisoformat(ev["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            continue
        pnl = ev.get("pnl_pct_lev", 0.0) or 0.0
        if d not in daily_map:
            daily_map[d] = {"date": d, "realized": 0.0, "unrealized": 0.0}
        daily_map[d]["realized"] += pnl

    daily_pnl = sorted(daily_map.values(), key=lambda x: x["date"])

    return {
        "unrealized_pnl_pct": round(unrealized_pnl, 2),
        "realized_pnl_pct": round(realized_pnl * 100, 2),
        "total_realized_pnl_pct": round(total_realized * 100, 2),
        "open_count": len(open_evs),
        "closed_count": len(all_closed),
        "wins": wins,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "daily_pnl": daily_pnl,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@api_bp.route("/summary")
def summary():
    events = _read_ledger()
    open_evs = _current_open_positions(events)
    summary_data = _compute_summary(open_evs, events)
    return jsonify(summary_data)


@api_bp.route("/positions")
def positions():
    events = _read_ledger()
    open_evs = _current_open_positions(events)
    data = _build_positions_data(open_evs)
    return jsonify(data)


@api_bp.route("/history")
def history():
    events = _read_ledger()
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    symbol_filter = request.args.get("symbol", "", type=str).upper()
    reason_filter = request.args.get("reason", "", type=str).lower()

    # Collect all closed events (deduplicate by id -- keep the close event)
    closed: list[dict] = []
    seen_ids: set[int] = set()
    for ev in events:
        if ev.get("status") == "closed":
            eid = int(ev["id"])
            if eid not in seen_ids:
                seen_ids.add(eid)
                closed.append(ev)

    # Build trade records
    trades = []
    for ev in closed:
        entry_price = float(ev["entry_price"])
        exit_price = float(ev["exit_price"])
        lev = float(ev.get("leverage", 3.0))
        pnl_lev = ev.get("pnl_pct_lev", 0.0) or 0.0

        # Compute bars_in_trade
        bars_in_trade = 0
        try:
            entry_ms = _parse_ts(ev["entry_ts"])
            exit_ms = _parse_ts(ev["exit_ts"])
            bars_in_trade = max(0, int((exit_ms - entry_ms) // (15 * 60 * 1000)))
        except Exception:
            pass

        trade = {
            "id": int(ev["id"]),
            "symbol": _strip_symbol(ev["symbol"]),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_reason": ev.get("exit_reason", ""),
            "pnl_pct_lev": round(pnl_lev * 100, 2),
            "entry_ts": ev["entry_ts"],
            "exit_ts": ev["exit_ts"],
            "bars_in_trade": bars_in_trade,
            "max_fav": None,
            "max_adv": None,
        }

        # Filters
        if symbol_filter and symbol_filter not in trade["symbol"]:
            continue
        if reason_filter and reason_filter != trade["exit_reason"].lower():
            continue

        trades.append(trade)

    # Sort by exit_ts descending (most recent first)
    trades.sort(key=lambda t: t["exit_ts"], reverse=True)

    total = len(trades)
    start = (page - 1) * per_page
    end = start + per_page
    page_trades = trades[start:end]

    return jsonify({
        "trades": page_trades,
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@api_bp.route("/klines/<symbol>")
def klines(symbol):
    since = request.args.get("since", None)
    bars = request.args.get("bars", 48, type=int)

    # Fetch klines from Binance fapi
    api_sym = symbol.upper().replace("/USDT", "").replace(":USDT", "") + "USDT"

    try:
        params = {
            "symbol": api_sym,
            "interval": "15m",
            "limit": min(bars, 500),
        }
        if since:
            try:
                params["startTime"] = int(since)
            except ValueError:
                pass  # invalid since, skip

        r = requests.get(FAPI, params=params, timeout=10)
        r.raise_for_status()
        raw = r.json()
    except Exception as e:
        log.warning("klines fetch failed %s: %s", symbol, e)
        return jsonify({"symbol": symbol, "klines": [], "markers": []})

    klines_out = []
    for row in raw:
        klines_out.append({
            "t": int(row[0]),
            "o": float(row[1]),
            "h": float(row[2]),
            "l": float(row[3]),
            "c": float(row[4]),
            "v": float(row[5]),
        })

    # Build markers from the ledger for this symbol
    events = _read_ledger()
    markers = []
    for ev in events:
        if _strip_symbol(ev["symbol"]).upper() == symbol.upper():
            try:
                ev_ts = _parse_ts(ev["entry_ts"])
            except Exception:
                continue
            if ev.get("status") == "open":
                markers.append({
                    "time": ev_ts,
                    "position": "aboveBar",
                    "color": "#0ecb81",
                    "shape": "arrowUp",
                    "text": f"Entry @ {float(ev['entry_price'])}",
                })
                # SL marker
                markers.append({
                    "time": ev_ts,
                    "position": "belowBar",
                    "color": "#f6465d",
                    "shape": "arrowDown",
                    "text": f"SL @ {float(ev['sl_price'])}",
                })
            elif ev.get("status") == "closed":
                try:
                    exit_ts = _parse_ts(ev["exit_ts"])
                except Exception:
                    continue
                markers.append({
                    "time": exit_ts,
                    "position": "belowBar",
                    "color": "#f6465d",
                    "shape": "arrowDown",
                    "text": f"Exit @ {float(ev['exit_price'])} ({ev.get('exit_reason', '')})",
                })

    return jsonify({
        "symbol": symbol,
        "klines": klines_out,
        "markers": markers,
    })