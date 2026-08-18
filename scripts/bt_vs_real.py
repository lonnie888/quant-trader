"""Backtest vs Real: replicate daemon's pump_pullback entry logic vs real trades on 8/7-8/9.

Replicates daemon.py entry conditions:
  1. Signal active at bar close (state==1 / 0->1 transition)
  2. Fresh pump >=13% in last 12 bars (新泵检测)
  3. Not in external 24h cooldown (after SL exit)
  4. No existing open position for the symbol
  Entry at latest closed bar close; SL/TP checked intrabar; hold for hold_bars.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings
from quant_trader.data.storage.parquet_store import ParquetStore

settings = load_settings()
store = ParquetStore(settings.data.storage_dir)

LEVERAGE = 5.0
FEE_RATE = 0.0004
SLIPPAGE = 0.0005
COOLDOWN_EXTERNAL_HOURS = 24  # external cooldown after SL exit

# Task-specified params
PARAMS = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 48,
    "cooldown": 96,             # 24h = 96 bars of 15m (task spec)
    "stop_loss_pct": 0.12,
    "take_profit_pct": 0.25,    # 25% TP
    "side": "long_only",
}

# Live-actual params (from ledger: TP=30%, strategy cooldown=12 bars)
PARAMS_LIVE = dict(PARAMS)
PARAMS_LIVE["take_profit_pct"] = 0.30
PARAMS_LIVE["cooldown"] = 12

TARGET_SYMBOLS = [
    "ACE", "HFT", "STG", "GWEI", "C98", "BICO", "TST", "BTW", "TUT",
    "BSB", "CYS", "BLESS", "MMT", "SYN", "DODOX", "BEAT", "1000CAT",
    "SKYAI", "BLUAI", "COOKIE", "SAGA", "MUBARAK", "IOTX", "ON", "BMT",
    "CATI", "XAN",
]


def extract_real_trades():
    """Extract all valid closed trades for target symbols opened 8/7-8/9."""
    ledger_path = ROOT / "reports" / "paper" / "positions.jsonl"
    events = []
    with open(ledger_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    real_trades = []
    for ev in events:
        if ev["status"] != "closed":
            continue
        sym = ev["symbol"].split("/")[0]
        if sym not in TARGET_SYMBOLS:
            continue
        entry_ts = ev.get("entry_ts", "")
        if not entry_ts or entry_ts[:10] not in ("2026-08-07", "2026-08-08", "2026-08-09"):
            continue
        if ev.get("exit_reason") in ("circuit_breaker", "stale_cleanup", "manual"):
            continue
        pnl = ev.get("pnl_pct_lev")
        if pnl is None:
            continue
        real_trades.append({
            "symbol": sym, "id": ev["id"], "entry_ts": entry_ts,
            "exit_ts": ev.get("exit_ts", ""), "entry_price": ev.get("entry_price", 0),
            "exit_price": ev.get("exit_price", 0), "exit_reason": ev.get("exit_reason", ""),
            "pnl_pct_lev": pnl * 100, "open_day": entry_ts[:10],
        })
    return real_trades


def generate_signals(df: pd.DataFrame, params: dict) -> pd.Series:
    """Identical logic to PumpPullbackStrategy.generate_signals."""
    pump_window = int(params["pump_window"])
    pump_threshold = float(params["pump_threshold"])
    pullback_min = float(params["pullback_min"])
    pullback_max = float(params["pullback_max"])
    vol_shrink = float(params.get("vol_shrink", 0.80))
    vol_recover = float(params.get("vol_recover", 1.0))
    trigger_pct = float(params.get("trigger_pct", 0.0))
    pump_lookback = int(params.get("pump_lookback", 96))
    cooldown = int(params["cooldown"])
    ema_period = int(params.get("ema_period", 9))
    hold_bars = int(params["hold_bars"])
    stop_loss_pct = float(params["stop_loss_pct"])
    take_profit_pct = float(params["take_profit_pct"])

    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values
    n = len(df)

    if n < max(pump_window + ema_period, 20):
        return pd.Series(0, index=df.index)

    ema = pd.Series(close).ewm(span=ema_period, adjust=False).mean().values

    state = np.zeros(n, dtype=int)
    cur = 0; held = 0; bars_since_exit = cooldown
    pump_high = 0.0; pump_bar_idx = -1; pump_vol = 0.0; entry_price = 0.0

    for i in range(n):
        if cur == 0:
            if i >= pump_window:
                win_high = high[i - pump_window + 1: i + 1].max()
                base_close = close[i - pump_window + 1]
                if base_close > 0 and (win_high / base_close - 1.0) >= pump_threshold:
                    local_idx = i - pump_window + 1 + int(np.argmax(high[i - pump_window + 1: i + 1]))
                    if local_idx > pump_bar_idx:
                        pump_high = win_high; pump_bar_idx = local_idx; pump_vol = vol[i - pump_window + 1: i + 1].max()
            if pump_bar_idx < 0 or i - pump_bar_idx > pump_lookback:
                state[i] = 0; bars_since_exit += 1; continue
            retr = 1.0 - close[i] / pump_high if pump_high > 0 else 0.0
            if not (pullback_min <= retr <= pullback_max):
                state[i] = 0; bars_since_exit += 1; continue
            recent_vol = vol[max(0, i - 3): i + 1].mean()
            if pump_vol > 0 and recent_vol > vol_shrink * pump_vol:
                state[i] = 0; bars_since_exit += 1; continue
            if trigger_pct > 0 and close[i] < ema[i] * (1 + trigger_pct):
                state[i] = 0; bars_since_exit += 1; continue
            short_avg = vol[max(0, i - 6): i + 1].mean()
            if vol_recover > 1.0 and short_avg < vol_recover * recent_vol:
                state[i] = 0; bars_since_exit += 1; continue
            if bars_since_exit < cooldown:
                state[i] = 0; bars_since_exit += 1; continue
            cur = 1; held = hold_bars; bars_since_exit = 0; entry_price = close[i]; state[i] = cur
        else:
            if entry_price > 0:
                if low[i] <= entry_price * (1 - stop_loss_pct):
                    cur = 0; held = 0; bars_since_exit = 0; state[i] = 0; continue
                if take_profit_pct > 0 and high[i] >= entry_price * (1 + take_profit_pct):
                    cur = 0; held = 0; bars_since_exit = 0; state[i] = 0; continue
            held -= 1
            if held <= 0:
                cur = 0; bars_since_exit = 0
            state[i] = cur

    return pd.Series(state, index=df.index).astype(int)


def run_backtest(params: dict, label: str) -> dict:
    """Replicate daemon entry logic for target symbols on 8/7-8/9."""
    DATE_START = pd.Timestamp("2026-08-07", tz='UTC')
    DATE_END = pd.Timestamp("2026-08-09 23:59:59", tz='UTC')
    LOOKBACK = pd.Timestamp("2026-07-25", tz='UTC')  # enough history for pump detection

    all_trades: list[tuple[str, dict]] = []
    per_symbol: dict[str, list[dict]] = {}

    for sym in TARGET_SYMBOLS:
        df = store.load(f"{sym}_USDT_USDT", "15m")
        if df.empty:
            continue
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        df_full = df[df.index >= LOOKBACK].copy()
        if len(df_full) < 100:
            continue

        sigs = generate_signals(df_full, params)
        s = sigs.values
        close = df_full["close"].values
        high = df_full["high"].values
        low = df_full["low"].values
        idx = df_full.index
        n = len(df_full)

        pw = int(params["pump_window"])
        pt = float(params["pump_threshold"])

        trades = []
        in_pos = False
        entry_p = 0.0; entry_i = 0; held = 0
        sl_p = 0.0; tp_p = 0.0
        cooldown_until = pd.Timestamp.min.tz_localize('UTC')
        tp_enabled = float(params["take_profit_pct"]) > 0

        for i in range(n):
            ts = idx[i]
            if ts < DATE_START:
                continue

            if not in_pos:
                # daemon: signal active at bar close
                if s[i] != 1:
                    continue
                # fresh pump check: pump >=13% in last 12 bars
                if i >= pw:
                    win_high = high[i - pw + 1: i + 1].max()
                    base_close = close[i - pw]
                    pump_pct = (win_high / base_close - 1.0) if base_close > 0 else 0
                    if pump_pct < pt:
                        continue
                else:
                    continue
                # external 24h cooldown after SL
                if ts < cooldown_until:
                    continue
                # open at bar close
                in_pos = True
                entry_p = close[i] * (1 + SLIPPAGE)
                entry_i = i
                held = 0
                sl_p = entry_p * (1 - float(params["stop_loss_pct"]))
                tp_p = entry_p * (1 + float(params["take_profit_pct"])) if tp_enabled else 0
            else:
                held += 1
                exit_reason = None; exit_p = None
                if low[i] <= sl_p:
                    exit_p = sl_p * (1 - SLIPPAGE); exit_reason = "sl"
                elif tp_enabled and high[i] >= tp_p:
                    exit_p = tp_p * (1 - SLIPPAGE); exit_reason = "tp"
                elif held >= int(params["hold_bars"]):
                    exit_p = close[i] * (1 - SLIPPAGE); exit_reason = "time"
                elif s[i] == 0:
                    exit_p = close[i] * (1 - SLIPPAGE); exit_reason = "signal"

                if exit_reason is not None:
                    pnl = (exit_p - entry_p) / entry_p * LEVERAGE - FEE_RATE * 2
                    trades.append({
                        "entry_ts": str(idx[entry_i]), "exit_ts": str(idx[i]),
                        "entry_price": round(entry_p, 8), "exit_price": round(exit_p, 8),
                        "exit_reason": exit_reason, "pnl_pct_lev": round(pnl * 100, 2),
                        "bars_held": held, "day": str(idx[entry_i])[:10],
                    })
                    in_pos = False
                    if exit_reason == "sl":
                        # external 24h cooldown after stop-loss
                        cooldown_until = idx[i] + pd.Timedelta(hours=COOLDOWN_EXTERNAL_HOURS)

        if trades:
            per_symbol[sym] = trades
            for t in trades:
                all_trades.append((sym, t))

    all_trades.sort(key=lambda x: x[1]["entry_ts"])

    per_sym_pnl: dict[str, list[float]] = defaultdict(list)
    for sym, t in all_trades:
        per_sym_pnl[sym].append(t["pnl_pct_lev"])

    total_pnl = sum(t[1]["pnl_pct_lev"] for t in all_trades)
    wins = sum(1 for t in all_trades if t[1]["pnl_pct_lev"] > 0)
    total_n = len(all_trades)

    sym_stats = {}
    for sym in TARGET_SYMBOLS:
        pnls = per_sym_pnl.get(sym, [])
        n = len(pnls); w = sum(1 for v in pnls if v > 0)
        sym_stats[sym] = {
            "trades": n, "wins": w, "losses": n - w,
            "win_rate": round(w / n * 100, 1) if n else 0,
            "total_pnl": round(sum(pnls), 2), "avg_pnl": round(sum(pnls) / n, 2) if n else 0,
        }

    return {
        "label": label, "params": params,
        "total_trades": total_n, "total_pnl": round(total_pnl, 2),
        "wins": wins, "losses": total_n - wins,
        "win_rate": round(wins / total_n * 100, 1) if total_n else 0,
        "per_symbol": sym_stats, "all_trades": all_trades,
    }


# ── Main ──
print("=" * 150)
print("  实盘记录 (2026-08-07 ~ 2026-08-09, 排除 circuit_breaker/stale_cleanup/manual)")
print("=" * 150)
real_trades = extract_real_trades()
real_by_sym: dict[str, list[dict]] = defaultdict(list)
for t in real_trades:
    real_by_sym[t["symbol"]].append(t)

real_pnl = sum(t["pnl_pct_lev"] for t in real_trades)
real_wins = sum(1 for t in real_trades if t["pnl_pct_lev"] > 0)
real_n = len(real_trades)

print(f"  实盘: {real_n}笔, 胜{real_wins}, 负{real_n-real_wins}, 胜率{real_wins/real_n*100:.1f}%, 总盈亏{real_pnl:+.2f}%")
for t in sorted(real_trades, key=lambda x: x["entry_ts"]):
    print(f"    {t['symbol']:>10} | {t['entry_ts'][:19]} | {t['exit_ts'][:19]} | {t['exit_reason']:>10} | pnl={t['pnl_pct_lev']:+.2f}% | entry={t['entry_price']:.6f}")

print("\n" + "=" * 150)
print("  回测 (task-spec: TP=25%, cooldown=24h/96bars, 含新泵检测+24h外部冷却+单仓)")
print("=" * 150)
bt = run_backtest(PARAMS, "task_spec")
print(f"  回测任务参数: {bt['total_trades']}笔, 胜{bt['wins']}, 负{bt['losses']}, 胜率{bt['win_rate']}%, 总盈亏{bt['total_pnl']:+.2f}%")

print("\n" + "=" * 150)
print("  回测 (live-params: TP=30%, strategy cooldown=12bars, 含新泵检测+24h外部冷却+单仓)")
print("=" * 150)
btl = run_backtest(PARAMS_LIVE, "live_actual")
print(f"  回测实盘参数: {btl['total_trades']}笔, 胜{btl['wins']}, 负{btl['losses']}, 胜率{btl['win_rate']}%, 总盈亏{btl['total_pnl']:+.2f}%")

# ── Comparison by symbol ──
print("\n" + "=" * 150)
print("  📊 逐币种对比 (回测任务参数 vs 回测实盘参数 vs 实盘)")
print("=" * 150)
print(f"  {'币种':>10} {'回测TP25%':>22} {'回测TP30%':>22} {'实盘':>22}")
print("  " + "-" * 82)

all_syms = set()
for t in real_trades: all_syms.add(t["symbol"])
all_syms.update(bt["per_symbol"].keys())
all_syms.update(btl["per_symbol"].keys())

for sym in sorted(all_syms):
    r = real_by_sym.get(sym, [])
    b = bt["per_symbol"].get(sym)
    bl = btl["per_symbol"].get(sym)

    r_s = f"{len(r)}笔/{sum(t['pnl_pct_lev'] for t in r):+.1f}%" if r else "—"
    b_s = f"{b['trades']}笔/{b['total_pnl']:+.1f}%/{b['win_rate']:.0f}%" if b else "—"
    bl_s = f"{bl['trades']}笔/{bl['total_pnl']:+.1f}%/{bl['win_rate']:.0f}%" if bl else "—"
    print(f"  {sym:>10} {b_s:>22} {bl_s:>22} {r_s:>22}")

# ── Aggregate summary ──
print()
print("=" * 150)
print("  📊 汇总")
print("=" * 150)
def row(label, n, w, l, wr, pnl):
    print(f"  {label:<40} trades={n:>3}  wins={w:>2}  losses={l:>2}  wr={wr:>5.1f}%  total_pnl={pnl:>+8.2f}%")

row("实盘", real_n, real_wins, real_n-real_wins, real_wins/real_n*100 if real_n else 0, real_pnl)
row("回测(TP25%,cooldown24h)", bt["total_trades"], bt["wins"], bt["losses"], bt["win_rate"], bt["total_pnl"])
row("回测(TP30%,cooldown3h)", btl["total_trades"], btl["wins"], btl["losses"], btl["win_rate"], btl["total_pnl"])

# ── Detailed backtest trades ──
for label, res in [("任务参数(TP25%)", bt), ("实盘参数(TP30%)", btl)]:
    print(f"\n  详细回测交易 [{label}]")
    for sym, t in res["all_trades"]:
        print(f"    {sym:>10} | {t['entry_ts'][:19]} | {t['exit_ts'][:19]} | {t['exit_reason']:>8} | pnl={t['pnl_pct_lev']:+.2f}% | entry={t['entry_price']:.6f}")

# ── Save ──
out = {
    "实盘": {"total_trades": real_n, "wins": real_wins, "losses": real_n-real_wins,
             "win_rate": round(real_wins/real_n*100,1) if real_n else 0, "total_pnl": round(real_pnl,2),
             "per_symbol": {s: [{"entry_ts": t["entry_ts"], "exit_ts": t["exit_ts"],
                                  "exit_reason": t["exit_reason"], "pnl_pct_lev": t["pnl_pct_lev"]} for t in ts]
                            for s, ts in real_by_sym.items()}},
    "回测_TP25_cooldown24h": bt,
    "回测_TP30_cooldown3h": btl,
}
out_path = ROOT / "reports" / "paper" / "bt_vs_real.json"
with open(out_path, "w") as f:
    json.dump(out, f, indent=2, ensure_ascii=False)
print(f"\n✅ 保存到 {out_path}")