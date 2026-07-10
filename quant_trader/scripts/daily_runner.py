"""Daily runner: the production flow for "tomorrow's trade".

Steps:
  1. Scan today's top-N gainers from Binance USDT-perp (or as-of a past date)
  2. Pull 15m klines for the last `lookback_days` (default 2) ending at as-of
  3. Apply each strategy variant to each symbol
  4. If a strategy has an open long signal in the LAST bar -> paper-trade it
  5. Aggregate: for each symbol, only the conservative (shortest hold) variant is kept
  6. Append the day's signals to a paper-trade log (JSONL) and write a daily Markdown summary
  7. Persist open events to the paper ledger (positions.jsonl) after a risk check

Usage:
  python -m quant_trader.scripts.daily_runner
  python -m quant_trader.scripts.daily_runner --dry-run
  python -m quant_trader.scripts.daily_runner --as-of 2026-07-01  # replay for past date
  python -m quant_trader.scripts.daily_runner --refresh-data     # pull latest 15m before scanning
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.data.fetcher.binance_client import BinanceClient  # noqa: E402
from quant_trader.data.fetcher.gainers_scanner import Gainer, scan_gainers  # noqa: E402
from quant_trader.data.fetcher.ohlcv_downloader import download_ohlcv  # noqa: E402
from quant_trader.data.storage.parquet_store import ParquetStore  # noqa: E402
from quant_trader.execution.paper_ledger import (  # noqa: E402
    evaluate_risk,
    get_all_positions,
    get_open_positions,
    open_position,
)
from quant_trader.strategy.generator.auto_strategy import generate_instances  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daily_runner")


def _entry_states(signal_series):
    if signal_series is None or signal_series.empty:
        return 0, -1, -1
    s = signal_series.values
    last_entry = -1
    last_exit = -1
    prev = 0
    for i, v in enumerate(s):
        if v == 1 and prev == 0:
            last_entry = i
        elif v == 0 and prev == 1:
            last_exit = i
        prev = v
    return int(s[-1]) if len(s) else 0, last_entry, last_exit


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategies", default="config/strategies.yaml")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--log-dir", default="reports/paper")
    p.add_argument("--as-of", help="YYYY-MM-DD; replay as if run on this date")
    p.add_argument("--gainers-file", help="use gainers_history.json for replay (date = as-of)")
    p.add_argument("--show-all-variants", action="store_true")
    p.add_argument("--refresh-data", action="store_true",
                   help="call refresh_fapi to pull latest 7d 15m klines for top-N gainers before scanning")
    p.add_argument("--refresh-lookback", type=int, default=7,
                   help="days to pull when --refresh-data is set")
    p.add_argument("--ignore-risk", action="store_true",
                   help="bypass the risk gate (still records the open in the ledger)")
    args = p.parse_args()

    import pandas as pd

    settings = load_settings(args.config)
    bin_cfg = settings.binance
    uni_cfg = settings.universe
    data_cfg = settings.data
    risk_cfg = settings.risk

    as_of = args.as_of or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    as_of_dt = datetime.strptime(as_of, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    log.info("as-of date: %s", as_of)

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    summary_path = log_dir / f"{as_of}.md"
    jsonl_path = log_dir / f"{as_of}.jsonl"
    positions_path = log_dir / "positions.jsonl"

    client = BinanceClient(api_key=bin_cfg.api_key, api_secret=bin_cfg.api_secret,
                           testnet=bool(bin_cfg.testnet))
    try:
        if args.gainers_file:
            with open(args.gainers_file, "r", encoding="utf-8") as _f:
                _gh = json.load(_f)
            day_top = _gh["days"].get(as_of, [])
            if not day_top:
                log.error("no gainers for %s in %s", as_of, args.gainers_file)
                return
            gainers = [
                Gainer(symbol=c["symbol"], last=c.get("close", 0),
                       pct_change_24h=c["pct"] * 100, quote_volume_24h=c["qvol"])
                for c in day_top[: uni_cfg.top_n]
            ]
            log.info("using historical gainers for %s: %s", as_of, [g.symbol for g in gainers])
        else:
            log.info("scanning top %d gainers...", uni_cfg.top_n)
            gainers = scan_gainers(
                client,
                quote=uni_cfg.quote,
                top_n=uni_cfg.top_n,
                min_quote_volume_24h=float(uni_cfg.min_quote_volume_24h),
                exclude=uni_cfg.exclude,
            )
            if not gainers:
                log.error("no gainers found")
                return
        log.info("gainers: %s", [g.symbol for g in gainers])

        symbols = [g.symbol for g in gainers]

        if args.refresh_data:
            from quant_trader.scripts.refresh_fapi import fetch_all
            from datetime import datetime as _dt, timezone as _tz, timedelta as _td
            end_dt = _dt.now(_tz.utc) if not args.as_of else (
                _dt.strptime(args.as_of, "%Y-%m-%d").replace(tzinfo=_tz.utc)
                + _td(days=1) - _td(milliseconds=1)
            )
            start_dt = end_dt - _td(days=args.refresh_lookback)
            start_ms = int(start_dt.timestamp() * 1000)
            end_ms = int(end_dt.timestamp() * 1000)
            log.info("refreshing %d symbols via fapi public endpoint, lookback=%dd",
                     len(symbols), args.refresh_lookback)
            _store = ParquetStore(data_cfg.storage_dir)
            for sym in symbols:
                api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                try:
                    df_new = fetch_all(api_sym, start_ms, end_ms)
                except Exception as e:
                    log.warning("refresh failed %s: %s", sym, e)
                    continue
                if df_new.empty:
                    log.warning("refresh empty %s", sym)
                    continue
                _store.save(sym, data_cfg.timeframes[0], df_new)
            log.info("refresh done")

        end = as_of_dt + timedelta(days=1) - timedelta(milliseconds=1)
        start = end - timedelta(days=int(data_cfg.lookback_days) + 1)
        log.info("downloading %d days of %s for %d symbols ending %s",
                 data_cfg.lookback_days, data_cfg.timeframes, len(symbols), end.isoformat())
        store = ParquetStore(data_cfg.storage_dir)
        for sym in symbols:
            for tf in data_cfg.timeframes:
                try:
                    df = download_ohlcv(client, sym, tf, lookback_days=int(data_cfg.lookback_days) + 1)
                except Exception as e:
                    log.warning("download failed %s %s: %s", sym, tf, e)
                    continue
                if args.as_of and not df.empty:
                    df = df[df.index <= end]
                if df.empty:
                    log.warning("no data for %s %s (as-of)", sym, tf)
                    continue
                store.save(sym, tf, df)

        instances = generate_instances(args.strategies)
        log.info("strategy variants: %d", len(instances))

        all_signals: list[dict] = []
        for sym in symbols:
            df = store.load(sym, data_cfg.timeframes[0])
            if df.empty:
                continue
            for name, params, strat in instances:
                try:
                    sigs = strat.generate_signals(df)
                except Exception as e:
                    log.warning("signal gen failed %s %s: %s", sym, name, e)
                    continue
                cur, last_entry, _ = _entry_states(sigs)
                if cur == 1 and last_entry >= 0:
                    entry_bar = df.iloc[last_entry]
                    last_bar = df.iloc[-1]
                    all_signals.append({
                        "timestamp": entry_bar.name.isoformat() if hasattr(entry_bar.name, "isoformat") else str(entry_bar.name),
                        "symbol": sym,
                        "timeframe": data_cfg.timeframes[0],
                        "strategy": name,
                        "params": params,
                        "side": "long",
                        "action": "open",
                        "price": float(entry_bar["close"]),
                        "decision_ts": last_bar.name.isoformat() if hasattr(last_bar.name, "isoformat") else str(last_bar.name),
                        "decision_price": float(last_bar["close"]),
                        "since_entry_bars": len(df) - 1 - last_entry,
                        "df_rows": len(df),
                        "as_of": as_of,
                    })
    finally:
        client.close()

    if not args.show_all_variants:
        keep: dict[str, dict] = {}
        for s in all_signals:
            sym = s["symbol"]
            hold = s["params"].get("hold_bars", 24)
            if sym not in keep or hold < keep[sym]["params"].get("hold_bars", 24):
                keep[sym] = s
        signals_log = sorted(keep.values(), key=lambda x: -x["price"])
    else:
        signals_log = all_signals

    # ----- risk gate -----
    leverage = float(settings.backtest.leverage)
    risk_check = {
        "initial_capital": float(settings.backtest.initial_capital),
        "max_position_pct": float(risk_cfg.max_position_pct),
        "max_total_exposure": float(risk_cfg.max_total_exposure),
        "daily_loss_limit": float(risk_cfg.daily_loss_limit),
        "max_concurrent": int(risk_cfg.max_concurrent),
    }
    blocked: list[dict] = []
    accepted: list[dict] = []
    if not args.dry_run and not args.ignore_risk:
        all_events = get_all_positions(positions_path)
        allowed, reason = evaluate_risk(all_events, **risk_check)
        log.info("risk gate: allowed=%s reason=%s", allowed, reason or "-")
        if not allowed:
            # halt: pass the day's gate reason down to each per-symbol attempt
            for s in signals_log:
                ev = open_position(
                    symbol=s["symbol"], strategy=s["strategy"], params=s["params"],
                    entry_ts=s["timestamp"], entry_price=s["price"], leverage=leverage,
                    open_day=as_of, log_path=positions_path,
                    risk_check={**risk_check, "max_concurrent": 0},
                )
            blocked = signals_log
            signals_log = []
        else:
            for s in signals_log:
                ev = open_position(
                    symbol=s["symbol"], strategy=s["strategy"], params=s["params"],
                    entry_ts=s["timestamp"], entry_price=s["price"], leverage=leverage,
                    open_day=as_of, log_path=positions_path, risk_check=risk_check,
                )
                if ev is not None and ev.status == "open":
                    accepted.append(s)
                    log.info("paper ledger: opened id=%d for %s", ev.id, s["symbol"])
                elif ev is not None and ev.status == "blocked":
                    blocked.append(s)
                    log.info("paper ledger: blocked id=%d for %s reason=%s",
                             ev.id, s["symbol"], ev.block_reason)
        # write daily signal jsonl regardless (audit trail)
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for s in signals_log:
                f.write(json.dumps(s, default=str) + "\n")
        # also append the blocked/accepted set for the day
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for s in accepted:
                row = dict(s); row["action"] = "accepted"; f.write(json.dumps(row, default=str) + "\n")
            for s in blocked:
                row = dict(s); row["action"] = "blocked"
                row["block_reason"] = reason or "n/a"
                f.write(json.dumps(row, default=str) + "\n")
        log.info("appended %d accepted, %d blocked to %s", len(accepted), len(blocked), jsonl_path)
    elif not args.dry_run and args.ignore_risk:
        # bypass: still record opens
        for s in signals_log:
            ev = open_position(
                symbol=s["symbol"], strategy=s["strategy"], params=s["params"],
                entry_ts=s["timestamp"], entry_price=s["price"], leverage=leverage,
                open_day=as_of, log_path=positions_path,
            )
            if ev is not None and ev.status == "open":
                log.info("paper ledger: opened id=%d for %s (risk bypass)", ev.id, s["symbol"])
        with open(jsonl_path, "a", encoding="utf-8") as f:
            for s in signals_log:
                f.write(json.dumps(s, default=str) + "\n")

    # ----- summary markdown -----
    open_pos = get_open_positions(positions_path)
    lines = [
        f"# Daily Trader - {as_of}",
        "",
        f"_Generated as-of {as_of}_",
        "",
        f"**Top {len(gainers)} gainers (24h)**:",
        "",
    ]
    for g in gainers:
        lines.append(f"- `{g.symbol}` pct={g.pct_change_24h:.2f}% qvol=${g.quote_volume_24h/1e6:.1f}M")
    lines += [
        "",
        f"**Open signals ({len(accepted)} accepted, {len(blocked)} blocked, {len(signals_log)} raw)**:",
        "",
        "| symbol | strategy | params | price | since_entry |",
        "| --- | --- | --- | ---: | ---: |",
    ]
    for s in (accepted or signals_log):
        params = s["params"]
        pstr = ", ".join(f"{k}={v}" for k, v in params.items() if k != "side")
        lines.append(f"| {s['symbol']} | {s['strategy']} | {pstr} | {s['price']:.6f} | {s['since_entry_bars']} bars |")
    if blocked:
        lines += [
            "",
            f"**Blocked by risk ({len(blocked)}):**",
            "",
            "| symbol | reason |",
            "| --- | --- |",
        ]
        for s in blocked:
            r = s.get("block_reason", "n/a")
            lines.append(f"| {s['symbol']} | {r} |")
    lines += [
        "",
        f"**Open paper positions ({len(open_pos)}):**",
        "",
        "| id | symbol | strategy | entry_ts | entry_price | sl | tp |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: |",
    ]
    for pp in open_pos:
        tp_s = "%.6f" % pp["tp_price"] if pp["tp_price"] is not None else "-"
        lines.append("| %d | %s | %s | %s | %.6f | %.6f | %s |" % (
            pp["id"], pp["symbol"], pp["strategy"], pp["entry_ts"],
            pp["entry_price"], pp["sl_price"], tp_s,
        ))
    summary_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("wrote summary: %s", summary_path)
    # Feishu daily summary (interactive card)
    try:
        from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
        feishu = FeishuNotifier()
        gainer_str = ", ".join(g.symbol.replace("/USDT:USDT", "") for g in gainers[:5])
        if len(gainers) > 5:
            gainer_str += f" ... +{len(gainers)-5} more"
        daily_card = FeishuCardBuilder.make_daily_summary(
            as_of=as_of,
            gainer_str=gainer_str,
            accepted=len(accepted),
            blocked=len(blocked),
            open_pos=len(open_pos),
        )
        feishu.send_card(daily_card)
    except Exception:
        pass

    if not signals_log and not accepted and not blocked:
        log.warning("no open signals on %s", as_of)


if __name__ == "__main__":
    main()