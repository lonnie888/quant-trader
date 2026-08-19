"""REST API routes for the Quant Trader Web Dashboard."""

import json
import logging
import time
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

    # 模拟盘权益计算（复利）：初始资金 + 每笔已平仓盈亏(保证金×杠杆收益)
    # 每笔: 保证金 = 当前权益 × max_position_pct, 盈亏 = 保证金 × pnl_pct_lev
    # 未平仓: 浮盈 = 保证金 × unrealized pnl
    from quant_trader.config import load_settings as _ls
    try:
        _s = _ls()
        _initial = float(_s.backtest.initial_capital)
        _margin_pct = float(getattr(_s.risk, "max_position_pct", 0.10) or 0.10)
    except Exception:
        _initial = 100.0
        _margin_pct = 0.10

    _equity = _initial
    _eq_curve = [{"date": today, "equity": round(_equity, 2)}]
    for ev in sorted(all_closed, key=lambda x: x.get("exit_ts", "")):
        pnl_lev = ev.get("pnl_pct_lev", 0.0) or 0.0
        _equity += _equity * _margin_pct * pnl_lev
        try:
            _d = datetime.fromisoformat(ev["exit_ts"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            _d = today
        _eq_curve.append({"date": _d, "equity": round(_equity, 2)})

    # 未平仓浮盈（对权益的影响）
    _u_equity = _equity
    for p in positions:
        _u_equity += _equity * _margin_pct * p["pnl_pct_lev"]

    return {
        "unrealized_pnl_pct": round(unrealized_pnl, 2),
        "realized_pnl_pct": round(realized_pnl * 100, 2),
        "total_realized_pnl_pct": round(total_realized * 100, 2),
        "initial_capital": round(_initial, 2),
        "equity": round(_u_equity, 2),          # 当前模拟权益（含浮盈）
        "equity_realized": round(_equity, 2),   # 已实现权益（不含浮盈）
        "equity_curve": _eq_curve,              # 权益曲线
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
    # Add initial capital so front-end can compute paper equity
    from quant_trader.config import load_settings
    _s = load_settings()
    summary_data["initial_capital"] = float(_s.backtest.initial_capital)
    summary_data["mode"] = "paper"
    return jsonify(summary_data)


@api_bp.route("/mode", methods=["GET", "POST"])
def mode():
    """Get or set the web display mode (real | paper)."""
    from . import mode as _mode
    if request.method == "POST":
        new_mode = request.json.get("mode") if request.json else None
        if new_mode not in ("real", "paper"):
            return jsonify({"error": "invalid mode, must be 'real' or 'paper'"}), 400
        _mode.set_mode(new_mode)
        return jsonify({"mode": new_mode, "ok": True})
    return jsonify({"mode": _mode.get_mode()})


@api_bp.route("/real-summary")
def real_summary():
    """Real account summary from Binance API."""
    from quant_trader.config import load_settings
    import hmac, hashlib
    _s = load_settings()
    api_key = getattr(_s.demo_trading, "api_key", "")
    api_secret = getattr(_s.demo_trading, "api_secret", "")
    proxy = getattr(_s, "proxy", None)
    if not api_key or not api_secret:
        return jsonify({"error": "API key not configured"}), 400

    try:
        from quant_trader.execution.real_account import get_realtime_summary, load_history
        data = get_realtime_summary(api_key, api_secret, proxy)
        history = load_history()

        # Compute today's realized PnL
        import calendar
        today = time.strftime("%Y-%m-%d", time.gmtime())
        today_realized = 0.0
        ts = int(time.time() * 1000)
        start_of_day = calendar.timegm(time.strptime(today, "%Y-%m-%d")) * 1000
        params = {"timestamp": str(ts), "recvWindow": "10000", "limit": "500", "startTime": str(start_of_day)}
        q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        proxies = {"http": proxy, "https": proxy} if proxy else None
        r = requests.get(
            f"https://fapi.binance.com/fapi/v1/income?{q}&signature={sig}",
            headers={"X-MBX-APIKEY": api_key}, proxies=proxies, timeout=10
        )
        if r.status_code == 200:
            incomes = r.json()
            for inc in incomes:
                if isinstance(inc, dict) and inc.get("incomeType") in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                    today_realized += float(inc.get("income", 0))

        # Compute initial equity from history
        initial_equity = 10.0  # default
        if len(history) > 0:
            initial_equity = history[0].get("totalWalletBalance", 10.0)

        data["todayRealizedPnl"] = round(today_realized, 4)
        data["todayRealizedPct"] = round(today_realized / (data["totalWalletBalance"] or 1) * 100, 2)
        data["totalReturnPct"] = round((data["totalWalletBalance"] - initial_equity) / initial_equity * 100, 2)
        data["totalReturnUsdt"] = round(data["totalWalletBalance"] - initial_equity, 2)
        return jsonify(data)
    except Exception as ex:
        log.warning("real summary failed: %s", ex)
        return jsonify({"error": str(ex)}), 500


@api_bp.route("/real-equity")
def real_equity():
    """Real account equity history for chart."""
    from quant_trader.execution.real_account import load_history
    history = load_history()
    # Return equity curve data
    curve = []
    for snap in history:
        curve.append({
            "t": snap.get("date", ""),
            "ts": snap.get("timestamp", 0),
            "equity": snap.get("totalWalletBalance", 0),
            "available": snap.get("availableBalance", 0),
            "positions": snap.get("positionCount", 0),
        })
    return jsonify(curve)


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


@api_bp.route("/real-history")
def real_history():
    """Real account trade history from Binance userTrades (每笔SELL=一条独立平仓记录)."""
    from quant_trader.config import load_settings
    import hmac, hashlib, calendar
    _s = load_settings()
    api_key = getattr(_s.demo_trading, "api_key", "")
    api_secret = getattr(_s.demo_trading, "api_secret", "")
    proxy = getattr(_s, "proxy", None)
    if not api_key or not api_secret:
        return jsonify({"error": "API key not configured"}), 400

    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 20, type=int)
    symbol_filter = request.args.get("symbol", "", type=str).upper()
    days = request.args.get("days", 7, type=int)

    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {"X-MBX-APIKEY": api_key}
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000

        def _signed_get(url, extra_params):
            params = dict(extra_params)
            params["timestamp"] = str(now_ms)
            params["recvWindow"] = "10000"
            q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f"{url}?{q}&signature={sig}", headers=headers, proxies=proxies, timeout=15)
            return r

        # Fetch userTrades (获取所有平仓)
        r = _signed_get("https://fapi.binance.com/fapi/v1/userTrades",
                        {"startTime": str(start_ms), "limit": "1000"})
        user_trades = r.json() if r.status_code == 200 else []
        if not isinstance(user_trades, list):
            return jsonify({"error": "invalid response", "trades": [], "total": 0})

        # 只取平仓记录（SELL, 有realizedPnl）
        # 同时间同symbol的多笔SELL聚合为一条记录（部分平仓）
        from collections import defaultdict
        close_map = defaultdict(lambda: {"qty": 0.0, "value": 0.0, "pnl": 0.0, "price": 0.0, "ts": 0, "count": 0})
        for ut in user_trades:
            if not isinstance(ut, dict):
                continue
            side = ut.get("side", "")
            pnl = float(ut.get("realizedPnl", 0))
            # 只取已平仓的SELL记录（有realizedPnl的）
            if side != "SELL" or abs(pnl) < 0.0001:
                continue
            sym = ut.get("symbol", "")
            ts = ut.get("time", 0)
            qty = float(ut.get("qty", 0))
            price = float(ut.get("price", 0))
            # 按symbol+时间戳分组（同一秒内的合并为一条）
            key = f"{sym}|{ts}"
            c = close_map[key]
            c["qty"] += qty
            c["value"] += qty * price
            c["pnl"] += pnl
            c["ts"] = ts
            c["sym"] = sym
            c["count"] += 1

        # 获取最近一笔BUY作为入场价
        buy_map = {}
        for ut in user_trades:
            if not isinstance(ut, dict) or ut.get("side") != "BUY":
                continue
            sym = ut.get("symbol", "")
            ts = ut.get("time", 0)
            qty = float(ut.get("qty", 0))
            price = float(ut.get("price", 0))
            key = f"{sym}|{ts // 10000}"  # 按10秒窗口分组
            if key not in buy_map or ts > buy_map[key]["ts"]:
                buy_map[key] = {"ts": ts, "qty": qty, "price": price, "sym": sym}

        # 获取income记录匹配手续费和资金费
        r2 = _signed_get("https://fapi.binance.com/fapi/v1/income",
                         {"startTime": str(start_ms), "limit": "1000"})
        incomes = r2.json() if r2.status_code == 200 else []
        # 按symbol+日期聚合手续费和资金费
        comm_by_sym_day = defaultdict(float)
        fund_by_sym_day = defaultdict(float)
        for inc in incomes:
            if not isinstance(inc, dict):
                continue
            it = inc.get("incomeType", "")
            val = float(inc.get("income", 0))
            sym = inc.get("symbol", "")
            day = time.strftime("%Y-%m-%d", time.gmtime(inc.get("time", 0) / 1000))
            sd = f"{sym}|{day}"
            if it == "COMMISSION":
                comm_by_sym_day[sd] += val
            elif it == "FUNDING_FEE":
                fund_by_sym_day[sd] += val

        # 构建交易列表
        trades = []
        for key, c in sorted(close_map.items(), key=lambda x: x[1]["ts"]):
            sym = c["sym"]
            if symbol_filter and symbol_filter not in sym:
                continue
            avg_price = c["value"] / c["qty"] if c["qty"] > 0 else 0
            day = time.strftime("%Y-%m-%d", time.gmtime(c["ts"] / 1000))
            sd = f"{sym}|{day}"
            commission = comm_by_sym_day.get(sd, 0.0)
            funding = fund_by_sym_day.get(sd, 0.0)
            net = c["pnl"] + commission + funding

            # 找入场价
            entry_price = None
            for bk, bv in sorted(buy_map.items(), key=lambda x: x[1]["ts"], reverse=True):
                if bv["sym"] == sym and bv["ts"] < c["ts"]:
                    entry_price = bv["price"]
                    break

            trades.append({
                "symbol": sym.replace("USDT", ""),
                "entry_price": round(entry_price, 6) if entry_price else None,
                "exit_price": round(avg_price, 6),
                "qty": int(c["qty"]),
                "realizedPnl": round(c["pnl"], 4),
                "commission": round(commission, 4),
                "funding": round(funding, 4),
                "netPnl": round(net, 4),
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(c["ts"] / 1000)),
                "ts": c["ts"],
            })

        # 按时间倒序
        trades.sort(key=lambda t: t["ts"], reverse=True)

        total = len(trades)
        start = (page - 1) * per_page
        end = start + per_page

        return jsonify({
            "trades": trades[start:end],
            "total": total,
            "page": page,
            "per_page": per_page,
            "days": days,
        })
    except Exception as ex:
        log.warning("real history failed: %s", ex)
        return jsonify({"error": str(ex)}), 500


@api_bp.route("/analysis")
def analysis():
    """Paper-ledger analysis data (same shape as /real-analysis)."""
    from collections import defaultdict
    events = _read_ledger()
    days = request.args.get("days", 7, type=int)

    # Collect closed trades
    closes = []
    seen_ids = set()
    for ev in events:
        if ev.get("status") == "closed":
            eid = int(ev["id"])
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            pnl_lev = float(ev.get("pnl_pct_lev", 0) or 0)
            closes.append({
                "sym": _strip_symbol(ev["symbol"]),
                "ts": _parse_ts(ev.get("exit_ts", ev.get("entry_ts", ""))),
                "day": (ev.get("exit_ts") or ev.get("entry_ts") or "")[:10],
                "pnl": pnl_lev,
                "reason": ev.get("exit_reason", "time"),
            })

    # Filter by days window
    now_ms = time.time() * 1000
    start_ms = now_ms - days * 24 * 3600 * 1000
    closes = [c for c in closes if c["ts"] >= start_ms]

    total_trades = len(closes)
    wins = sum(1 for c in closes if c["pnl"] > 0)
    losses = sum(1 for c in closes if c["pnl"] < 0)
    total_pnl = sum(c["pnl"] for c in closes)
    win_rate = wins / total_trades * 100 if total_trades else 0

    by_sym = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
    for c in closes:
        s = c["sym"]
        by_sym[s]["trades"] += 1
        by_sym[s]["pnl"] += c["pnl"]
        if c["pnl"] > 0:
            by_sym[s]["wins"] += 1
    top_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)[:15]
    worst_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"])[:15]

    by_day = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for c in closes:
        d = c["day"]
        by_day[d]["pnl"] += c["pnl"]
        by_day[d]["trades"] += 1
        if c["pnl"] > 0:
            by_day[d]["wins"] += 1
    daily_pnl = [{"date": d, "pnl": round(v["pnl"], 4), "trades": v["trades"]}
                 for d, v in sorted(by_day.items())]

    days_sorted = sorted(by_day.keys())
    cumulative = 0.0
    equity_curve = []
    for d in days_sorted:
        cumulative += by_day[d]["pnl"]
        equity_curve.append({"date": d, "equity": round(cumulative, 4)})

    # Exit reason distribution
    by_reason = defaultdict(int)
    for c in closes:
        by_reason[c["reason"]] += 1

    pnl_buckets = {"<-2": 0, "-2~-1": 0, "-1~-0.5": 0, "-0.5~0": 0,
                   "0~0.5": 0, "0.5~1": 0, "1~2": 0, ">2": 0}
    for c in closes:
        p = c["pnl"]
        if p < -2: pnl_buckets["<-2"] += 1
        elif p < -1: pnl_buckets["-2~-1"] += 1
        elif p < -0.5: pnl_buckets["-1~-0.5"] += 1
        elif p < 0: pnl_buckets["-0.5~0"] += 1
        elif p < 0.5: pnl_buckets["0~0.5"] += 1
        elif p < 1: pnl_buckets["0.5~1"] += 1
        elif p < 2: pnl_buckets["1~2"] += 1
        else: pnl_buckets[">2"] += 1

    return jsonify({
        "summary": {
            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1),
            "total_pnl": round(total_pnl, 4),
            "days": days,
        },
        "exit_reasons": {
            "stop_loss": by_reason.get("SL", by_reason.get("sl", 0)),
            "take_profit": by_reason.get("TP", by_reason.get("tp", 0)),
            "time": by_reason.get("time", 0),
        },
        "daily_pnl": daily_pnl,
        "equity_curve": equity_curve,
        "top_symbols": [{"symbol": s, "pnl": round(v["pnl"], 4), "trades": v["trades"], "wins": v["wins"]}
                        for s, v in top_symbols],
        "worst_symbols": [{"symbol": s, "pnl": round(v["pnl"], 4), "trades": v["trades"], "wins": v["wins"]}
                          for s, v in worst_symbols],
        "pnl_distribution": [{"range": k, "count": v} for k, v in pnl_buckets.items()],
    })


@api_bp.route("/real-analysis")
def real_analysis():
    """Analysis data for the dashboard charts."""
    import hmac, hashlib
    from collections import defaultdict
    from quant_trader.config import load_settings
    _s = load_settings()
    api_key = getattr(_s.demo_trading, "api_key", "")
    api_secret = getattr(_s.demo_trading, "api_secret", "")
    proxy = getattr(_s, "proxy", None)
    if not api_key or not api_secret:
        return jsonify({"error": "API key not configured"}), 400

    days = request.args.get("days", 7, type=int)
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        headers = {"X-MBX-APIKEY": api_key}
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - days * 24 * 3600 * 1000

        def _sg(url, p):
            pp = dict(p)
            pp["timestamp"] = str(now_ms)
            pp["recvWindow"] = "10000"
            q = "&".join(f"{k}={v}" for k, v in sorted(pp.items()))
            sig = hmac.new(api_secret.encode(), q.encode(), hashlib.sha256).hexdigest()
            r = requests.get(f"{url}?{q}&signature={sig}", headers=headers, proxies=proxies, timeout=15)
            return r.json() if r.status_code == 200 else []

        # Fetch userTrades + income
        user_trades = _sg("https://fapi.binance.com/fapi/v1/userTrades",
                          {"startTime": str(start_ms), "limit": "1000"})
        incomes = _sg("https://fapi.binance.com/fapi/v1/income",
                      {"startTime": str(start_ms), "limit": "1000"})

        # Extract close trades (SELL with realized PnL)
        closes = []
        for ut in user_trades if isinstance(user_trades, list) else []:
            if not isinstance(ut, dict) or ut.get("side") != "SELL":
                continue
            pnl = float(ut.get("realizedPnl", 0))
            if abs(pnl) < 0.0001:
                continue
            sym = ut.get("symbol", "")
            ts = ut.get("time", 0)
            qty = float(ut.get("qty", 0))
            price = float(ut.get("price", 0))
            closes.append({
                "sym": sym.replace("USDT", ""),
                "ts": ts,
                "day": time.strftime("%Y-%m-%d", time.gmtime(ts / 1000)),
                "pnl": pnl,
                "qty": qty,
                "price": price,
            })

        # 1. Exit reason distribution
        # We can't get exact reason from Binance API, but we can infer:
        # - If PnL > 0 and price moved up significantly → likely take_profit
        # - If PnL < 0 and price moved down significantly → likely stop_loss
        # - Otherwise → time exit
        # For now, categorize by PnL sign
        total_trades = len(closes)
        wins = sum(1 for c in closes if c["pnl"] > 0)
        losses = sum(1 for c in closes if c["pnl"] < 0)
        total_pnl = sum(c["pnl"] for c in closes)
        win_rate = wins / total_trades * 100 if total_trades else 0

        # 2. By symbol
        by_sym = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        for c in closes:
            s = c["sym"]
            by_sym[s]["trades"] += 1
            by_sym[s]["pnl"] += c["pnl"]
            if c["pnl"] > 0:
                by_sym[s]["wins"] += 1
        top_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"], reverse=True)[:15]
        worst_symbols = sorted(by_sym.items(), key=lambda x: x[1]["pnl"])[:15]

        # 3. Daily PnL series
        by_day = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
        for c in closes:
            d = c["day"]
            by_day[d]["pnl"] += c["pnl"]
            by_day[d]["trades"] += 1
            if c["pnl"] > 0:
                by_day[d]["wins"] += 1
        daily_pnl = [{"date": d, "pnl": round(v["pnl"], 4), "trades": v["trades"]}
                     for d, v in sorted(by_day.items())]

        # 4. Cumulative equity curve
        days_sorted = sorted(by_day.keys())
        cumulative = 0.0
        equity_curve = []
        for d in days_sorted:
            cumulative += by_day[d]["pnl"]
            equity_curve.append({"date": d, "equity": round(cumulative, 4)})

        # 5. PnL distribution (bucket by size)
        pnl_buckets = {"<-2": 0, "-2~-1": 0, "-1~-0.5": 0, "-0.5~0": 0,
                       "0~0.5": 0, "0.5~1": 0, "1~2": 0, ">2": 0}
        for c in closes:
            p = c["pnl"]
            if p < -2: pnl_buckets["<-2"] += 1
            elif p < -1: pnl_buckets["-2~-1"] += 1
            elif p < -0.5: pnl_buckets["-0.5~0"] += 1
            elif p < 0: pnl_buckets["-0.5~0"] += 1
            elif p < 0.5: pnl_buckets["0~0.5"] += 1
            elif p < 1: pnl_buckets["0.5~1"] += 1
            elif p < 2: pnl_buckets["1~2"] += 1
            else: pnl_buckets[">2"] += 1

        return jsonify({
            "summary": {
                "total_trades": total_trades,
                "wins": wins,
                "losses": losses,
                "win_rate": round(win_rate, 1),
                "total_pnl": round(total_pnl, 4),
                "days": days,
            },
            "exit_reasons": {
                "stop_loss": losses,
                "take_profit": wins,
                "time": 0,  # Binance doesn't provide exit reason
            },
            "daily_pnl": daily_pnl,
            "equity_curve": equity_curve,
            "top_symbols": [{"symbol": s, "pnl": round(v["pnl"], 4), "trades": v["trades"], "wins": v["wins"]}
                           for s, v in top_symbols],
            "worst_symbols": [{"symbol": s, "pnl": round(v["pnl"], 4), "trades": v["trades"], "wins": v["wins"]}
                             for s, v in worst_symbols],
            "pnl_distribution": [{"range": k, "count": v} for k, v in pnl_buckets.items()],
        })
    except Exception as ex:
        log.warning("real analysis failed: %s", ex)
        return jsonify({"error": str(ex)}), 500


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