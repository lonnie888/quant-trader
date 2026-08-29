#!/usr/bin/env python3
"""分月验证币池稳定性: 每币 × 每月独立回测(100U), 输出月度收益矩阵.

用法:
    python scripts/monthly_stability.py --symbols EDEN,MAGMA,AKE,...  [--start 2026-01-01] [--end 2026-08-29]
输出: 屏幕矩阵 + reports/analysis/monthly_stability_<日期>.json
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pandas as pd

from backtest import load_data, generate_signals, run_backtest

SL, TP, HOLD = 0.139842, 0.214923, 66


def monthly_windows(start: str, end: str):
    """返回 [(month_label, start_ts, end_ts)]"""
    s = pd.Timestamp(start, tz="UTC").normalize()
    e = pd.Timestamp(end, tz="UTC").normalize()
    out = []
    cur = s
    while cur < e:
        nxt = (cur + pd.DateOffset(months=1))
        if nxt > e:
            nxt = e + pd.Timedelta(days=1)
        out.append((cur.strftime("%Y-%m"), cur, nxt))
        cur = nxt
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", required=True)
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-08-29")
    args = ap.parse_args()
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    months = monthly_windows(args.start, args.end)

    print(f"分月验证 {len(syms)} 币 x {len(months)} 月 (每月独立100U回测, 无过滤)")
    dd = load_data(target_syms=syms, timeframe="1h")
    print(f"数据加载: {len(dd)} 币")

    all_rows = []  # {sym, month, trades, win_rate, return_pct}
    for s in syms:
        if s not in dd:
            print(f"{s}: 无数据")
            continue
        for label, ms, me in months:
            sigs = generate_signals({s: dd[s]}, "pump_pullback_opt",
                                    test_start=ms, test_end=me)
            n_sig = sum(len(v) for v in sigs.values())
            if n_sig == 0:
                all_rows.append({"sym": s, "month": label, "trades": 0,
                                 "win_rate": None, "return_pct": 0.0})
                continue
            _, _, stats = run_backtest(
                sigs, {s: dd[s]}, initial_equity=100.0, leverage=5,
                sl_pct=SL, tp_pct=TP, hold_bars=HOLD,
                vol_threshold=999, vol_df=None,
                use_weekend_filter=False, use_bad_hours_filter=False,
            )
            all_rows.append({"sym": s, "month": label, "trades": stats["trades"],
                             "win_rate": round(stats["win_rate"], 1),
                             "return_pct": round(stats["return_pct"], 1)})
        print(f"  {s} done", flush=True)

    # 输出矩阵
    labels = [m[0] for m in months]
    mat = {s: {} for s in syms}
    for r in all_rows:
        mat[r["sym"]][r["month"]] = r

    print()
    print(f"{'币':<10}" + "".join(f"{m:>8}" for m in labels) + f"{'盈月':>5}{'最大亏':>8}")
    for s in syms:
        if s not in mat:
            continue
        line = f"{s:<10}"
        win_m = 0
        maxloss = 0
        for m in labels:
            r = mat[s].get(m)
            if r and r["trades"] > 0:
                v = r["return_pct"]
                line += f"{v:>7.1f}%"
                if v > 0:
                    win_m += 1
                if v < maxloss:
                    maxloss = v
            else:
                line += f"{'—':>8}"
        line += f"{win_m:>4}月"
        line += f"{maxloss:>7.1f}%"
        print(line)

    out = str(ROOT / "reports/analysis/monthly_stability_20260829.json")
    with open(out, "w") as f:
        json.dump({"months": labels, "rows": all_rows}, f, ensure_ascii=False, indent=1)
    print(f"\n已保存: {out}")


if __name__ == "__main__":
    main()