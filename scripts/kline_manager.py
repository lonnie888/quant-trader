"""
K线数据管理器 - 数据概况统计
列出所有币种的 K 线数据状态: 起止时间、行数、缺口
"""
import sys
import time
import pandas as pd
from pathlib import Path
sys.path.insert(0, '/vol1/1000/quant_trader')
from quant_trader.data.storage.parquet_store import ParquetStore


def scan_store(store_path='data_store', cache_path='/tmp/kline_scan_cache.parquet'):
    """扫描 data_store 所有币种的 K 线概况, 生成 DataFrame"""
    store = ParquetStore(store_path)
    t0 = time.time()
    records = []
    syms = store.list_symbols()
    for i, s in enumerate(syms):
        df = store.load(s, '15m')
        if df.empty:
            records.append({'symbol': s.replace('_USDT_USDT', ''),
                            'rows': 0, 'first': None, 'last': None,
                            'gaps_2h': 0, 'has_funding': False})
            continue
        # 缺口: 相邻 K 线间隔 > 2h (8根15m)
        dt = df.index.to_series().diff()
        gaps = int((dt > pd.Timedelta(hours=2)).sum())
        # funding 文件是否存在
        funding_path = Path(store_path) / s / 'funding.parquet'
        records.append({
            'symbol': s.replace('_USDT_USDT', ''),
            'rows': len(df),
            'first': df.index[0],
            'last': df.index[-1],
            'gaps_2h': gaps,
            'has_funding': funding_path.exists(),
        })
        if (i + 1) % 100 == 0:
            print(f'  扫描 {i+1}/{len(syms)} ({time.time()-t0:.0f}s)', file=sys.stderr)

    result = pd.DataFrame(records)
    result['start_date'] = pd.to_datetime(result['first']).dt.strftime('%Y-%m-%d')
    result['end_date'] = pd.to_datetime(result['last']).dt.strftime('%Y-%m-%d')
    result['span_days'] = (pd.to_datetime(result['last']) - pd.to_datetime(result['first'])).dt.days
    result = result.sort_values('symbol').reset_index(drop=True)
    if cache_path:
        result.to_parquet(cache_path)
    print(f'扫描完成: {len(result)} 币种, {time.time()-t0:.0f}s', file=sys.stderr)
    return result


if __name__ == '__main__':
    df = scan_store()
    pd.set_option('display.width', 200)
    print(df[['symbol', 'rows', 'start_date', 'end_date', 'span_days', 'gaps_2h', 'has_funding']].to_string(index=False))
    print()
    print('=== 汇总 ===')
    print(f'总币种: {len(df)}')
    print(f'有数据币种: {(df["rows"] > 0).sum()}')
    print(f'覆盖天数: {df["span_days"].median():.0f} 天 (中位数)')
    print(f'最小: {df["span_days"].min()} 天, 最大: {df["span_days"].max()} 天')
    print(f'有缺口(>2h)的币种: {(df["gaps_2h"] > 0).sum()}')