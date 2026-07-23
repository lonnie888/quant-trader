"""Backtest multi-level SL/TP:
- SL: -3% (full close)
- TP1: +6% (close 50%)
- TP2: +12% (close 30% of original)
- TP3: +18% (close remaining 20%)
"""
from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from quant_trader.config import load_settings
from quant_trader.data.storage.parquet_store import ParquetStore
from quant_trader.strategy.library.pump_pullback import PumpPullbackStrategy

settings = load_settings()
store = ParquetStore(settings.data.storage_dir)

LEVERAGE = 3.0
FEE_RATE = 0.0004
SLIPPAGE = 0.0005
MIN_BARS = 500
DAILY_LOSS_LIMIT = 0.50

BASE = {
    "pump_window": 12, "pump_threshold": 0.13,
    "pullback_min": 0.05, "pullback_max": 0.30,
    "vol_shrink": 0.80, "vol_recover": 1.0,
    "trigger_pct": 0.0, "ema_period": 12,
    "hold_bars": 24, "cooldown": 12,
    "stop_loss_pct": 0.10, "take_profit_pct": 0.30,
    "side": "long_only",
}


def find_pump_idx(close, window, threshold):
    n = len(close)
    out = []
    for i in range(window, n):
        past = close[i - window]
        if past <= 0: continue
        if (close[i] - past) / past >= threshold:
            out.append(i)
    return out


def backtest(sl_pct, tp_levels, hold_bars):
    """tp_levels: list of (pct_from_entry, close_fraction_of_original)
    Order matters: TP1 is first, etc. Remaining position continues.
    sl_pct: stop loss as fraction of entry (negative number in the trigger sense).
    """
    strategy = PumpPullbackStrategy(BASE)
    raw = []
    for sym, df in store.list_symbols().__iter__() if False else []:
        pass
    # Load symbols
    all_syms = store.list_symbols()
    symbols = []
    for sym in all_syms:
        df = store.load(sym, "15m")
        if not df.empty and len(df) >= MIN_BARS:
            symbols.append((sym, df))
    print(f"加载 {len(symbols)} 个币种", flush=True)

    for sym, df in symbols:
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index
        n = len(s)
        pump_idx = find_pump_idx(close, BASE["pump_window"], BASE["pump_threshold"])
        if not pump_idx:
            continue
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
                        continue
                    in_pos = True
                    entry_p = close[i] * (1 + SLIPPAGE)
                    entry_idx = i
                    sl_p = entry_p * (1 + sl_pct)  # sl_pct is negative
                    tp_prices = [(entry_p * (1 + p), frac) for p, frac in tp_levels]
                    held = 0
                    remaining = 1.0  # fraction of original position
                    trade_pnl = 0.0  # accumulated PnL across partial closes
            else:
                held += 1
                # Check SL first (full close)
                if low[i] <= sl_p:
                    exit_p = sl_p * (1 - SLIPPAGE)
                    trade_pnl += remaining * ((exit_p - entry_p) / entry_p) * LEVERAGE
                    raw.append((sym, {
                        "entry_ts": str(idx[entry_idx]),
                        "exit_ts": str(idx[i]),
                        "pnl_pct_lev": trade_pnl * 100 - FEE_RATE * 2 * 100,
                        "exit_reason": "sl",
                        "day": str(idx[entry_idx])[:10],
                    }))
                    in_pos = False
                    continue
                # Check TPs in order (TP1 first, then TP2, etc.)
                closed_this_bar = False
                for tp_idx, (tp_price, frac) in enumerate(tp_prices):
                    if remaining <= 1e-6:
                        break
                    if high[i] >= tp_price and remaining >= frac - 1e-6:
                        exit_p = tp_price * (1 - SLIPPAGE)
                        # Only pay fees on the fraction closed
                        trade_pnl += frac * ((exit_p - entry_p) / entry_p) * LEVERAGE - FEE_RATE * 2 * frac * 100
                        raw.append((sym, {
                            "entry_ts": str(idx[entry_idx]),
                            "exit_ts": str(idx[i]),
                            "pnl_pct_lev": frac * ((exit_p - entry_p) / entry_p) * LEVERAGE * 100 - FEE_RATE * 2 * frac * 100,
                            "exit_reason": f"tp{tp_idx+1}",
                            "day": str(idx[entry_idx])[:10],
                        }))
                        remaining -= frac
                        if remaining <= 1e-6:
                            in_pos = False
                            closed_this_bar = True
                            break
                if closed_this_bar:
                    continue
                # Hold bars expiry
                if held >= hold_bars or s[i] == 0:
                    exit_p = close[i] * (1 - SLIPPAGE)
                    if remaining > 0:
                        trade_pnl += remaining * ((exit_p - entry_p) / entry_p) * LEVERAGE
                        raw.append((sym, {
                            "entry_ts": str(idx[entry_idx]),
                            "exit_ts": str(idx[i]),
                            "pnl_pct_lev": trade_pnl * 100,
                            "exit_reason": "time",
                            "day": str(idx[entry_idx])[:10],
                        }))
                    in_pos = False
    raw.sort(key=lambda x: x[1]["entry_ts"])
    daily = defaultdict(float)
    passed = []
    blocked = 0
    for sym, t in raw:
        d = t["day"]
        if daily[d] <= -DAILY_LOSS_LIMIT:
            blocked += 1
            continue
        daily[d] += t["pnl_pct_lev"] / 100
        passed.append(t)
    return raw, passed, blocked


def report(name, raw, passed, blocked):
    print(f"\n{'='*80}\n{name}\n{'='*80}")
    pnls = [t["pnl_pct_lev"] for t in passed]
    if not pnls:
        print("  无交易")
        return
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    win_rate = len(wins) / len(pnls) * 100
    avg_win = np.mean(wins) if wins else 0
    avg_loss = abs(np.mean(losses)) if losses else 1
    pf = sum(wins) / abs(sum(losses)) if losses else float("inf")
    total_ret = sum(pnls)
    sharpe = (np.mean(pnls) / np.std(pnls)) * np.sqrt(len(pnls)) if len(pnls) > 1 and np.std(pnls) > 0 else 0
    cum = np.cumsum(pnls)
    peak = np.maximum.accumulate(cum)
    max_dd = np.min(cum - peak) if len(cum) else 0

    # Reason breakdown
    reasons = defaultdict(list)
    for t in passed:
        reasons[t["exit_reason"]].append(t["pnl_pct_lev"])
    reason_lines = [f"{k}: {len(v)} ({np.mean(v):+.2f}%)" for k, v in sorted(reasons.items())]

    print(f"  原始: {len(raw)} | 通过: {len(passed)} | 阻挡: {blocked} ({blocked/(len(raw)+blocked)*100:.1f}%)")
    print(f"  胜率: {win_rate:.1f}% | 盈亏比: {avg_win/avg_loss:.2f} | PF: {pf:.2f}")
    print(f"  总收益: {total_ret:+.1f}% | Sharpe: {sharpe:.3f} | 最大回撤: {max_dd:.1f}%")
    print(f"  退出原因: {' | '.join(reason_lines)}")


# === 加载数据一次 ===
all_syms = store.list_symbols()
symbols = []
for sym in all_syms:
    df = store.load(sym, "15m")
    if not df.empty and len(df) >= MIN_BARS:
        symbols.append((sym, df))
print(f"加载 {len(symbols)} 个币种")


# 单次跑会重新 load 数据，做个缓存
def backtest_cached(sl_pct, tp_levels, hold_bars):
    strategy = PumpPullbackStrategy(BASE)
    raw = []
    for sym, df in symbols:
        try:
            sigs = strategy.generate_signals(df)
        except Exception:
            continue
        if sigs.empty or sigs.sum() == 0:
            continue
        s = sigs.values
        close = df["close"].values
        high = df["high"].values
        low = df["low"].values
        idx = df.index
        n = len(s)
        pump_idx = find_pump_idx(close, BASE["pump_window"], BASE["pump_threshold"])
        if not pump_idx:
            continue
        in_pos = False
        for i in range(n):
            if not in_pos:
                if s[i] == 1:
                    if not any(0 <= i - pi <= 1 for pi in pump_idx):
                        continue
                    in_pos = True
                    entry_p = close[i] * (1 + SLIPPAGE)
                    entry_idx = i
                    sl_p = entry_p * (1 + sl_pct)
                    tp_prices = [(entry_p * (1 + p), frac) for p, frac in tp_levels]
                    held = 0
                    remaining = 1.0
            else:
                held += 1
                if low[i] <= sl_p:
                    exit_p = sl_p * (1 - SLIPPAGE)
                    pnl = remaining * ((exit_p - entry_p) / entry_p) * LEVERAGE - FEE_RATE * 2 * remaining
                    raw.append((sym, {
                        "entry_ts": str(idx[entry_idx]),
                        "exit_ts": str(idx[i]),
                        "pnl_pct_lev": pnl * 100,
                        "exit_reason": "sl",
                        "day": str(idx[entry_idx])[:10],
                    }))
                    in_pos = False
                    continue
                closed_this_bar = False
                for tp_idx, (tp_price, frac) in enumerate(tp_prices):
                    if remaining <= 1e-6:
                        break
                    if high[i] >= tp_price and remaining >= frac - 1e-6:
                        exit_p = tp_price * (1 - SLIPPAGE)
                        pnl = frac * ((exit_p - entry_p) / entry_p) * LEVERAGE - FEE_RATE * 2 * frac
                        raw.append((sym, {
                            "entry_ts": str(idx[entry_idx]),
                            "exit_ts": str(idx[i]),
                            "pnl_pct_lev": pnl * 100,
                            "exit_reason": f"tp{tp_idx+1}",
                            "day": str(idx[entry_idx])[:10],
                        }))
                        remaining -= frac
                        if remaining <= 1e-6:
                            in_pos = False
                            closed_this_bar = True
                            break
                if closed_this_bar:
                    continue
                if held >= hold_bars or s[i] == 0:
                    exit_p = close[i] * (1 - SLIPPAGE)
                    if remaining > 0:
                        pnl = remaining * ((exit_p - entry_p) / entry_p) * LEVERAGE
                        raw.append((sym, {
                            "entry_ts": str(idx[entry_idx]),
                            "exit_ts": str(idx[i]),
                            "pnl_pct_lev": pnl * 100,
                            "exit_reason": "time",
                            "day": str(idx[entry_idx])[:10],
                        }))
                    in_pos = False
    raw.sort(key=lambda x: x[1]["entry_ts"])
    daily = defaultdict(float)
    passed = []
    blocked = 0
    for sym, t in raw:
        d = t["day"]
        if daily[d] <= -DAILY_LOSS_LIMIT:
            blocked += 1
            continue
        daily[d] += t["pnl_pct_lev"] / 100
        passed.append(t)
    return raw, passed, blocked


# === 三组对比 ===
VARIANTS = {
    # 单 SL/TP（基线，策略默认值）
    "v0.4.0 baseline SL=-10% TP=+30%": (-0.10, [(0.30, 1.0)], 24),
    # 用户方案：3% 止损 + 3 档止盈
    "用户方案 SL=-3% TP1=+6%/50% TP2=+12%/30% TP3=+18%/20%": (-0.03, [(0.06, 0.50), (0.12, 0.30), (0.18, 0.20)], 24),
    # 变体 1：更紧 SL -2%
    "变体 SL=-2% TP1=+6%/50% TP2=+12%/30% TP3=+18%/20%": (-0.02, [(0.06, 0.50), (0.12, 0.30), (0.18, 0.20)], 24),
    # 变体 2：更紧 TP1 +4%
    "变体 SL=-3% TP1=+4%/50% TP2=+10%/30% TP3=+18%/20%": (-0.03, [(0.04, 0.50), (0.10, 0.30), (0.18, 0.20)], 24),
    # 变体 3：更宽 SL -5%
    "变体 SL=-5% TP1=+6%/50% TP2=+12%/30% TP3=+18%/20%": (-0.05, [(0.06, 0.50), (0.12, 0.30), (0.18, 0.20)], 24),
}

for name, (sl, tps, hb) in VARIANTS.items():
    raw, passed, blocked = backtest_cached(sl, tps, hb)
    report(name, raw, passed, blocked)
