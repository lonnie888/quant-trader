#!/usr/bin/env python3
"""2026 币池筛选: 对指定币逐一回测 pump_pullback_opt(1h), 按表现排序.

用法:
    python scripts/screen_2026_coins.py                      # 涨幅榜前50 (reports/analysis/gainers_top50_20260829.json)
    python scripts/screen_2026_coins.py --symbols A,B,C      # 指定币
    python scripts/screen_2026_coins.py --start 2026-01-01 --end 2026-08-29
输出: 屏幕表格 + reports/analysis/screen_2026_<日期>.json (含每币 stats)
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pandas as pd

from backtest import load_data, generate_signals, run_backtest

# PumpPullbackOpt 生产参数 (strategies.yaml 当前值)
OPT_PARAMS = dict(
    pump_window=12, pump_threshold=0.070738, pullback_min=0.071613,
    pullback_max=0.237939, vol_shrink=0.873908, vol_recover=1.0,
    trigger_pct=0.0, ema_period=12, hold_bars=66, cooldown=19,
    stop_loss_pct=0.139842, take_profit_pct=0.214923, pump_lookback=96,
    side="long_only",
)
SL = 0.139842
TP = 0.214923
HOLD = 66


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default="", help="逗号分隔, 空=涨幅榜前50")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-08-29")
    ap.add_argument("--min-trades", type=int, default=5, help="少于该笔数的币剔除显示可选")
    args = ap.parse_args()

    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        with open(str(ROOT / "reports/analysis/gainers_top50_20260829.json")) as f:
            syms = json.load(f)["symbols"]
    print(f"筛选 {len(syms)} 币, 窗口 {args.start} ~ {args.end}, 1h pump_pullback_opt (无过滤, 对齐Jesse)")

    test_start = pd.Timestamp(args.start, tz="UTC")
    test_end = pd.Timestamp(args.end, tz="UTC")

    t0 = time.time()
    dd = load_data(target_syms=syms, timeframe="1h")
    print(f"数据加载: {len(dd)} 币 ({time.time()-t0:.0f}s), 缺失: {[s for s in syms if s not in dd]}")

    results = []
    for s in syms:
        if s not in dd:
            results.append({"sym": s, "error": "no_data"})
            continue
        try:
            sigs = generate_signals({s: dd[s]}, "pump_pullback_opt",
                                    test_start=test_start, test_end=test_end)
            n_sig = sum(len(v) for v in sigs.values())
            if n_sig == 0:
                results.append({"sym": s, "signals": 0, "trades": 0, "note": "no_signal"})
                continue
            trades, equity, stats = run_backtest(
                sigs, {s: dd[s]}, initial_equity=100.0, leverage=5,
                sl_pct=SL, tp_pct=TP, hold_bars=HOLD,
                vol_threshold=999, vol_df=None,
                use_weekend_filter=False, use_bad_hours_filter=False,
            )
            results.append({
                "sym": s,
                "signals": n_sig,
                "trades": stats["trades"],
                "wins": stats["wins"],
                "losses": stats["losses"],
                "win_rate": round(stats["win_rate"], 1),
                "return_pct": round(stats["return_pct"], 1),
                "final_equity": round(stats["final_equity"], 2),
                "max_dd": round(stats["max_dd"], 1),
                "profit_factor": round(stats["profit_factor"], 2),
            })
        except Exception as e:
            results.append({"sym": s, "error": str(e)})

    # 排序: 有交易的按 return_pct 降序, 无交易/错误排后
    def keyf(r):
        if "return_pct" in r:
            return (-r["return_pct"], r["trades"])
        return (1e9, 0)

    results.sort(key=keyf)
    print()
    print(f"{'币':<12} {'交易':>5} {'胜率':>7} {'收益%':>10} {'回撤%':>7} {'盈亏比':>7}  备注")
    win_n = 0
    for r in results:
        if "return_pct" in r:
            mark = "✓" if r["return_pct"] > 0 else "✗"
            if r["return_pct"] > 0:
                win_n += 1
            print(f"{r['sym']:<12} {r['trades']:>5} {r['win_rate']:>6.1f}% {r['return_pct']:>9.1f}% "
                  f"{r['max_dd']:>6.1f}% {r['profit_factor']:>6.2f}  {mark}")
        else:
            print(f"{r['sym']:<12} {'-':>5} {'-':>7} {'-':>10} {'-':>7} {'-':>7}  {r.get('note','')}")
    n_traded = sum(1 for r in results if "return_pct" in r)
    print(f"\n有交易 {n_traded} 币, 盈利 {win_n} 币 ({win_n/max(n_traded,1)*100:.0f}%)")

    out = str(ROOT / f"reports/analysis/screen_2026_{pd.Timestamp.now():%Y%m%d}.json")
    with open(out, "w") as f:
        json.dump({"window": [args.start, args.end], "results": results}, f, ensure_ascii=False, indent=1)
    print(f"已保存: {out}")


if __name__ == "__main__":
    main()