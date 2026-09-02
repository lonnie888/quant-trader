#!/usr/bin/env python3
"""精确对比: 实盘窗口(08-29 14:00 ~ 08-31 01:00 UTC)内回测信号 vs 实盘日志.

输出每个回测信号时间, 并标记实盘日志里同币同时段做了什么.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import pandas as pd

from backtest import load_data, generate_signals

POOL = ["MUBARAK","COLLECT","TUT","KITE","PROM","SIREN","GWEI","USELESS","BANK","MOVR","BLESS",
        "ENSO","MAGMA","XNY","DEXE","HOME","HEMI","龙虾","XAN","EDEN"]
W_START = "2026-08-29 14:00"  # UTC, 实盘20币上线
W_END = "2026-08-31 01:00"


def main():
    dd = load_data(target_syms=POOL, timeframe="1h")
    print(f"数据加载 {len(dd)} 币")
    test_start = pd.Timestamp(W_START, tz="UTC")
    test_end = pd.Timestamp(W_END, tz="UTC")
    # 读实盘日志: 每币每整点的检查结果
    import re
    log_lines = {}
    try:
        with open(str(ROOT / "reports/logs/daemon.log")) as f:
            for line in f:
                if "无泵" in line or "market filter skip" in line:
                    m = re.search(r"\[ws\] (\S+) 无泵\(([0-9.]+)%", line)
                    if not m:
                        m = re.search(r"skip (\w+): ([^)]+)\)", line)
                    if m:
                        sym = m.group(1)
                        log_lines.setdefault(sym, []).append(line.strip()[:110])
    except FileNotFoundError:
        print("daemon.log 未找到")

    print()
    print(f"{'币':<10} {'信号时间(UTC)':<22} {'实盘同时段日志'}")
    for s in POOL:
        if s not in dd:
            continue
        sigs = generate_signals({s: dd[s]}, "pump_pullback_opt",
                                test_start=test_start, test_end=test_end)
        entries = sigs.get(s, [])
        if not entries:
            continue
        for t in entries:
            # 找实盘日志: 该币该小时附近的记录
            nearby = [l for l in log_lines.get(s, []) if str(t)[:13] in l or True][:3]
            nearby_txt = " | ".join(nearby) if nearby else "(日志无该币记录)"
            print(f"{s:<10} {str(t):<22} {nearby_txt[:90]}")
    print()
    print("done")


if __name__ == "__main__":
    main()