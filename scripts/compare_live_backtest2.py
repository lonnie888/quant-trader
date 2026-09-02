#!/usr/bin/env python3
"""对比: 数据补全后本地回测信号时间 vs Jesse 交叉验证时间."""
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
W0 = pd.Timestamp("2026-08-29 14:00", tz="UTC")
W1 = pd.Timestamp("2026-08-31 01:00", tz="UTC")

def main():
    dd = load_data(target_syms=POOL, timeframe="1h")
    print(f"数据加载 {len(dd)} 币")
    j = json.load(open("/tmp/jesse_crossval_20.json"))
    print(f"\n{'币':<10}{'本地信号时间':<22}{'Jesse开仓时间':<22}  一致?")
    both = only_local = only_jesse = 0
    for s in POOL:
        if s not in dd:
            continue
        sigs = generate_signals({s: dd[s]}, "pump_pullback_opt", test_start=W0, test_end=W1)
        local_t = [str(t)[:19] for t in sigs.get(s, [])]
        jd = j.get(s, (None, None))[1]
        jesse_t = [t[:19] for t in (jd["opened"] if jd else [])]
        mark = ""
        if local_t and jesse_t:
            both += 1
            mark = "✓" if local_t == jesse_t else "△时间差"
        elif local_t:
            only_local += 1
            mark = "← 仅本地"
        elif jesse_t:
            only_jesse += 1
            mark = "仅Jesse"
        else:
            mark = "都无"
        print(f"{s:<10}{str(local_t):<22}{str(jesse_t):<22}  {mark}")
    print(f"\n两边都有: {both} | 仅本地: {only_local} | 仅Jesse: {only_jesse}")

if __name__ == "__main__":
    main()
