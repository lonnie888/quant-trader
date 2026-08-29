#!/usr/bin/env python3
"""增量更新绿联 postgres 11币池 1m K线 (Binance fapi klines API).

回测前运行: 把 pg 里每币的最新 1m 数据补到当前时刻, 保证回测数据完整.
- 读取每币 pg MAX(timestamp), 只拉之后的增量, ON CONFLICT 防重 (可反复跑)
- 写入 Jesse 共用 candle 表 (exchange='Binance Perpetual Futures', timeframe='1m')

用法:
    python scripts/update_pg_klines.py                 # 11币池默认
    python scripts/update_pg_klines.py --symbols A,B   # 指定币
    python scripts/update_pg_klines.py --days 7        # 无记录时回拉天数(默认30)
"""
import argparse
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import requests
from psycopg2.extras import execute_values

PG = dict(host="192.168.1.142", port=15440, dbname="jesse_db",
          user="jesse_user", password="Jesse2025")
PROXY = {"http": "http://192.168.1.1:7890", "https": "http://192.168.1.1:7890"}
DEFAULT_SYMS = ["MUBARAK", "COLLECT", "TUT", "KITE", "PROM", "SIREN",
                "GWEI", "USELESS", "BANK", "MOVR", "BLESS"]


def fetch_klines(base: str, start_ms: int, end_ms: int) -> list[list]:
    """从 Binance fapi 拉 1m klines (分页 1500 根/次)."""
    rows: list[list] = []
    cur = start_ms
    while cur < end_ms:
        r = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": base + "USDT", "interval": "1m",
                    "startTime": cur, "endTime": end_ms, "limit": 1500},
            timeout=30, proxies=PROXY,
        )
        r.raise_for_status()
        data = r.json()
        if not data:
            break
        rows.extend(data)
        last = data[-1][0]
        if len(data) < 1500 or last >= end_ms:
            break
        cur = last + 1
        time.sleep(0.15)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMS))
    ap.add_argument("--days", type=int, default=30, help="无记录时回拉天数")
    args = ap.parse_args()
    syms = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)

    conn = psycopg2.connect(**PG, connect_timeout=10)
    cur = conn.cursor()
    total_all = 0
    for base in syms:
        pg_sym = base + "-USDT"
        cur.execute(
            "SELECT COALESCE(MAX(timestamp),0) FROM candle WHERE symbol=%s AND timeframe='1m'",
            (pg_sym,),
        )
        max_ts = cur.fetchone()[0]
        start = max_ts + 1 if max_ts else now_ms - args.days * 86_400_000
        if start >= now_ms:
            print(f"{base:10s} 已是最新 (pg 到 {datetime.fromtimestamp(max_ts/1000, timezone.utc):%Y-%m-%d %H:%M})")
            continue
        rows = fetch_klines(base, start, now_ms)
        if not rows:
            print(f"{base:10s} 无新数据")
            continue
        # 批量插入, 唯一索引 (exchange,symbol,timeframe,timestamp) 防重
        data = [
            (pg_sym, r[0], r[1], r[2], r[3], r[4], r[5])
            for r in rows
        ]
        execute_values(
            cur,
            "INSERT INTO candle (id, exchange, symbol, timeframe, timestamp, open, high, low, close, volume) "
            "VALUES %s "
            "ON CONFLICT (exchange, symbol, timeframe, timestamp) DO NOTHING",
            data,
            template="(gen_random_uuid(), 'Binance Perpetual Futures', %s, '1m', %s, %s, %s, %s, %s, %s)",
            page_size=1000,
        )
        n = cur.rowcount
        conn.commit()
        total_all += n
        new_max = rows[-1][0]
        print(f"{base:10s} 更新 {n:>6} 根  → pg 最新 {datetime.fromtimestamp(new_max/1000, timezone.utc):%Y-%m-%d %H:%M}")
    conn.close()
    print(f"\n完成: 共更新 {total_all} 根 1m")


if __name__ == "__main__":
    main()
