#!/usr/bin/env python3
"""排查涨幅榜前50币在绿联 pg 的数据覆盖."""
import datetime
import json

import psycopg2

LIST_PATH = "reports/analysis/gainers_top50_20260829.json"
OUT_PATH = "reports/analysis/gainers_top50_pg_check.json"


def dt(x):
    return datetime.datetime.utcfromtimestamp(x / 1000).strftime("%Y-%m-%d") if x else "-"


def main():
    with open(LIST_PATH) as f:
        data = json.load(f)
    syms = data["symbols"]
    gainers = data["gainers"]

    conn = psycopg2.connect(host="192.168.1.142", port=15440, dbname="jesse_db",
                            user="jesse_user", password="Jesse2025", connect_timeout=10)
    cur = conn.cursor()
    # 单次查询: 索引扫描 MIN/MAX (COUNT 用 pg_class 估算避免全扫)
    pg_syms = [s + "-USDT" for s in syms]
    cur.execute(
        "SELECT symbol, MIN(timestamp), MAX(timestamp) FROM candle "
        "WHERE timeframe='1m' AND symbol = ANY(%s) GROUP BY symbol",
        (pg_syms,),
    )
    cover = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    cur.execute("SELECT reltuples FROM pg_class WHERE relname='candle'")
    approx_total = int(cur.fetchone()[0])
    have, missing, rows_out = [], [], []
    for s in syms:
        g = gainers.get(s, 0)
        c = cover.get(s + "-USDT")
        if c:
            mn, mx = c
            cov = "full" if dt(mn) <= "2025-01-02" else dt(mn)
            have.append({"sym": s, "gain": g, "start": dt(mn), "end": dt(mx)})
            rows_out.append(f"{s:<12} 已有 起始:{cov:<11} {dt(mn)} ~ {dt(mx)} 涨幅{g:+.1f}%")
        else:
            missing.append(s)
            rows_out.append(f"{s:<12} 缺失 (涨幅{g:+.1f}%)")
    conn.close()

    with open(OUT_PATH, "w") as f:
        json.dump({"have": have, "missing": missing, "symbols": syms}, f, ensure_ascii=False, indent=1)

    print("\n".join(rows_out))
    print(f"\n=== 已有 {len(have)} 币 | 缺失 {len(missing)} 币 ===")
    print("缺失:", missing)


if __name__ == "__main__":
    main()