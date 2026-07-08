"""Daily recap: replay every open signal from prior days' paper-trade logs
and compute realized PnL using the actual subsequent 15m klines.

For each (symbol, strategy, params, entry_price, entry_time) entry:
  - fetch klines from entry_time to now via the public fapi endpoint
  - simulate the strategy's hold_bars with bar-internal SL/TP priority
  - pnl_pct = (exit - entry) / entry * leverage
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

from quant_trader.config import load_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("recap")


def load_paper_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
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


def fetch_subsequent_klines(symbol: str, start_ms: int) -> list[list]:
    """Fetch 15m klines via the fapi public endpoint (no auth needed)."""
    out: list[list] = []
    cursor = start_ms
    api_symbol = symbol.split("/")[0].split(":")[0] + "USDT"
    base = "https://fapi.binance.com/fapi/v1/klines"
    while True:
        r = requests.get(
            base,
            params={"symbol": api_symbol, "interval": "15m", "startTime": cursor, "limit": 1000},
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


def simulate_hold(klines: list[list], entry_ts_ms: int, entry_price: float,
                  hold_bars: int, leverage: float,
                  stop_loss_pct: float = 0.0,
                  take_profit_pct: float = 0.0) -> dict:
    """Walk klines bar-by-bar; first-of SL/TP/time exit wins.

    Returns a dict with ok flag, exit price + reason, pnl% (raw and lev),
    and max favorable / adverse excursion during the hold window.
    """
    if not klines:
        return {"ok": False, "reason": "no klines"}

    sl_price = entry_price * (1 - stop_loss_pct) if stop_loss_pct > 0 else None
    tp_price = entry_price * (1 + take_profit_pct) if take_profit_pct > 0 else None
    exit_ts_target = entry_ts_ms + hold_bars * 15 * 60 * 1000

    exit_price = None
    exit_ts = None
    exit_reason = "time"
    max_fav = 0.0
    max_adv = 0.0
    bars_held = 0

    for row in klines:
        ts, o, h, l, c, v = row
        if ts < entry_ts_ms:
            continue
        if entry_price > 0:
            fav = (h - entry_price) / entry_price
            adv = (l - entry_price) / entry_price
            if fav > max_fav:
                max_fav = fav
            if adv < max_adv:
                max_adv = adv

        if sl_price is not None and l <= sl_price:
            exit_price = sl_price
            exit_ts = ts
            exit_reason = "stop_loss"
            break
        if tp_price is not None and h >= tp_price:
            exit_price = tp_price
            exit_ts = ts
            exit_reason = "take_profit"
            break
        if ts >= exit_ts_target:
            exit_price = c
            exit_ts = ts
            exit_reason = "time"
            break
        bars_held += 1

    if exit_price is None:
        exit_price = klines[-1][4]
        exit_ts = klines[-1][0]
        exit_reason = "data_end"

    pnl_pct = (exit_price - entry_price) / entry_price if entry_price > 0 else 0.0
    pnl_lev = pnl_pct * leverage
    return {
        "ok": True,
        "entry_ts": entry_ts_ms,
        "entry_price": entry_price,
        "exit_ts": exit_ts,
        "exit_price": exit_price,
        "exit_reason": exit_reason,
        "bars_in_trade": bars_held,
        "pnl_pct_raw": pnl_pct,
        "pnl_pct_lev": pnl_lev,
        "max_favorable_pct": max_fav,
        "max_adverse_pct": max_adv,
        "win": pnl_lev > 0,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--log-dir", default="reports/paper")
    p.add_argument("--date", help="YYYY-MM-DD; default = yesterday")
    p.add_argument("--days-back", type=int, default=3, help="replay last N days of paper logs")
    p.add_argument("--default-stop-loss-pct", type=float, default=0.10,
                   help="used when a paper entry has no `stop_loss_pct` in params")
    p.add_argument("--default-take-profit-pct", type=float, default=0.0,
                   help="used when a paper entry has no `take_profit_pct` in params")
    args = p.parse_args()

    settings = load_settings(args.config)
    bt_cfg = settings.backtest
    leverage = float(bt_cfg.leverage)

    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        log.error("log dir %s does not exist", log_dir)
        return

    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")
    if args.date:
        # include this specific day in addition to days_back window
        days = [args.date] + [(now - timedelta(days=d)).strftime("%Y-%m-%d")
                              for d in range(1, args.days_back + 1)]
    else:
        days = [(now - timedelta(days=d)).strftime("%Y-%m-%d")
                for d in range(1, args.days_back + 1)]
    log_files = [(d, log_dir / f"{d}.jsonl") for d in days
                 if (log_dir / f"{d}.jsonl").exists()]

    if not log_files:
        log.error("no log files in last %d days", args.days_back)
        return
    log.info("replaying %d log file(s): %s", len(log_files), [d for d, _ in log_files])

    all_entries: list[tuple[str, dict]] = []
    for day, p in log_files:
        entries = load_paper_log(p)
        for e in entries:
            all_entries.append((day, e))
    log.info("total entries: %d", len(all_entries))

    recaps: list[dict] = []
    for day, e in all_entries:
        sym = e["symbol"]
        entry_price = e["price"]
        entry_ts = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00"))
        entry_ms = int(entry_ts.timestamp() * 1000)
        hold_bars = int(e["params"].get("hold_bars", 24))
        sl = float(e["params"].get("stop_loss_pct", args.default_stop_loss_pct))
        tp = float(e["params"].get("take_profit_pct", args.default_take_profit_pct))
        try:
            klines = fetch_subsequent_klines(sym, entry_ms)
        except Exception as exc:
            log.warning("fetch failed %s: %s", sym, exc)
            continue
        sim = simulate_hold(klines, entry_ms, entry_price, hold_bars, leverage,
                            stop_loss_pct=sl, take_profit_pct=tp)
        if not sim.get("ok"):
            continue
        recaps.append({
            "open_day": day,
            "symbol": sym,
            "strategy": e["strategy"],
            "params": e["params"],
            **sim,
        })

    if not recaps:
        log.warning("nothing to recap")
        return

    n = len(recaps)
    wins = sum(1 for r in recaps if r["win"])
    losses = n - wins
    avg_pnl = sum(r["pnl_pct_lev"] for r in recaps) / n
    avg_pnl_wins = (sum(r["pnl_pct_lev"] for r in recaps if r["win"]) / wins) if wins else 0.0
    avg_pnl_losses = (sum(r["pnl_pct_lev"] for r in recaps if not r["win"]) / losses) if losses else 0.0
    total_lev = sum(r["pnl_pct_lev"] for r in recaps)
    win_rate = wins / n

    by_strat: dict[str, list[dict]] = {}
    by_reason: dict[str, int] = {}
    for r in recaps:
        by_strat.setdefault(r["strategy"], []).append(r)
        by_reason[r["exit_reason"]] = by_reason.get(r["exit_reason"], 0) + 1

    out_path = log_dir / f"recap-{today}.md"
    lines = [
        f"# Recap - {today}",
        "",
        f"_Replayed {n} entries from {len(log_files)} day(s), SL={args.default_stop_loss_pct*100:.0f}% TP={args.default_take_profit_pct*100:.0f}%",
        "",
        f"**Overall**: win_rate={win_rate*100:.1f}% ({wins}W / {losses}L)  avg_pnl={avg_pnl*100:+.2f}%  total_lev={total_lev*100:+.2f}%",
        "",
        f"**Wins** avg_pnl={avg_pnl_wins*100:+.2f}%  |  **Losses** avg_pnl={avg_pnl_losses*100:+.2f}%",
        "",
        f"**Exit reasons**: " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())),
        "",
        "## Per trade",
        "",
        "| open_day | symbol | strategy | entry | exit | reason | pnl% (lev) | max_fav% | max_adv% | win |",
        "| --- | --- | --- | ---: | ---: | :---: | ---: | ---: | ---: | :---: |",
    ]
    for r in sorted(recaps, key=lambda x: -x["pnl_pct_lev"]):
        lines.append("| %s | %s | %s | %.6f | %.6f | %s | %+.2f | %+.2f | %+.2f | %s |" % (
            r["open_day"], r["symbol"], r["strategy"],
            r["entry_price"], r["exit_price"], r["exit_reason"],
            r["pnl_pct_lev"] * 100,
            r["max_favorable_pct"] * 100,
            r["max_adverse_pct"] * 100,
            "W" if r["win"] else "L",
        ))
    lines += [
        "",
        "## By strategy",
        "",
        "| strategy | n | win_rate | avg_pnl% | total% |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for strat, lst in sorted(by_strat.items(), key=lambda kv: -sum(r["pnl_pct_lev"] for r in kv[1])):
        ns = len(lst)
        ws = sum(1 for r in lst if r["win"])
        ap = sum(r["pnl_pct_lev"] for r in lst) / ns
        tp = sum(r["pnl_pct_lev"] for r in lst)
        lines.append("| %s | %d | %.1f%% | %+.2f | %+.2f |" % (strat, ns, ws/ns*100, ap*100, tp*100))

    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote %s", out_path)


if __name__ == "__main__":
    main()