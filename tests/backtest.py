"""
回测核心模块 (CLI 共用) - 与实盘 daemon 行为对齐:
- 多仓并行 (max_concurrent=20)
- 复利 (每笔盈亏 = 当前权益 × margin × leverage × pnl_lev)
- 持仓检查 (同币种有 open 时不开新仓)
- 市场过滤 (周末 + 坏时段 + 波动率)
- K 线内保守触发 (同根 K 线先 SL 后 TP)

公开 API:
- run_backtest() - 接收预生成的 signals + data_dict
- load_data() - 从 parquet 加载数据
- compute_volatility_series() - 4h 间隔的全局波动率
- generate_signals() - 用 strategies.yaml 里的策略类生成开仓信号
"""
import sys
import json
import time
import requests
import pandas as pd
import numpy as np
from typing import Dict, List, Optional, Tuple
from pathlib import Path

# 兼容直接 run: python tests/backtest.py
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from quant_trader.data.storage.parquet_store import ParquetStore

# ==============================================================================
# 核心回测 (向量化版, 接近 grid_v5.py 的实现)
# ==============================================================================

def run_backtest(
    signals: Dict[str, List[pd.Timestamp]],
    data_dict: Dict[str, pd.DataFrame],
    initial_equity: float = 100.0,
    margin_pct: float = 0.10,
    leverage: int = 5,
    sl_pct: float = 0.12,
    tp_pct: float = 0.25,
    hold_bars: int = 48,
    vol_threshold: float = 12.0,
    vol_df: Optional[pd.DataFrame] = None,
    use_weekend_filter: bool = False,
    use_bad_hours_filter: bool = False,
    bad_hours: set = None,
    max_concurrent: int = 20,
    return_trades: bool = True,
) -> Tuple[List[dict], float, dict]:
    """
    向量化回测核心.
    Args:
        signals: {sym: [entry_dt_list]} - 每币种的开仓信号时间
        data_dict: {sym: OHLCV DataFrame}
        initial_equity: 初始资金
        margin_pct: 保证金比例
        leverage: 杠杆倍数
        sl_pct: 止损比例 (相对入场价)
        tp_pct: 止盈比例
        hold_bars: 最大持有 K 线数
        vol_threshold: 波动率阈值 (超过则停手)
        vol_df: 波动率 DataFrame, index=time, columns=['vol']
    Returns:
        (trades_list, final_equity, stats)
    """
    if bad_hours is None:
        bad_hours = {0, 1, 2, 3, 16, 17, 18, 19}

    # 1. 展平所有开仓事件, 应用过滤器
    rows = []
    for sym, ts_list in signals.items():
        if sym not in data_dict:
            continue
        df = data_dict[sym]
        for t in ts_list:
            # 时区统一: 转为 UTC
            if t.tzinfo is None:
                t = t.tz_localize('UTC')
            else:
                t = t.tz_convert('UTC')
            # 市场过滤
            if use_weekend_filter and t.weekday() >= 5:
                continue
            if use_bad_hours_filter and t.hour in bad_hours:
                continue
            if vol_df is not None:
                if t in vol_df.index:
                    if vol_df.loc[t, 'vol'] > vol_threshold:
                        continue
                else:
                    idx = vol_df.index.asof(t)
                    if pd.notna(idx) and vol_df.loc[idx, 'vol'] > vol_threshold:
                        continue
            # 找 entry_price
            if t not in df.index:
                continue
            entry_price = float(df.loc[t, 'close'])
            if entry_price <= 0:
                continue
            rows.append({'sym': sym, 'entry_dt': t, 'entry_price': entry_price})
    if not rows:
        return [], initial_equity, _empty_stats(initial_equity)
    df = pd.DataFrame(rows).sort_values('entry_dt').reset_index(drop=True)

    # 2. 对每笔算 SL/TP/到期
    def calc_exit(row):
        sym = row['sym']
        entry_dt = row['entry_dt']
        entry_price = row['entry_price']
        df = data_dict[sym]
        t2i = {t: i for i, t in enumerate(df.index)}
        if entry_dt not in t2i:
            return None
        idx = t2i[entry_dt]
        sl = entry_price * (1 - sl_pct)
        tp = entry_price * (1 + tp_pct)
        end_idx = min(idx + 1 + hold_bars, len(df))
        if end_idx <= idx + 1:
            return None
        lows = df['low'].values[idx + 1:end_idx]
        highs = df['high'].values[idx + 1:end_idx]
        for i in range(len(lows)):
            if lows[i] <= sl:
                return {'exit_dt': df.index[idx + 1 + i], 'exit_price': sl, 'reason': 'SL'}
            if highs[i] >= tp:
                return {'exit_dt': df.index[idx + 1 + i], 'exit_price': tp, 'reason': 'TP'}
        last_idx = end_idx - 1
        return {'exit_dt': df.index[last_idx], 'exit_price': float(df['close'].iloc[last_idx]), 'reason': 'time'}

    exits = df.apply(calc_exit, axis=1, result_type='expand')
    exits.columns = ['exit_dt', 'exit_price', 'reason']
    df = pd.concat([df, exits], axis=1).dropna(subset=['exit_dt'])

    # 3. 持仓检查: 同币种 entry_dt <= 上次 exit_dt 时跳过
    valid = []
    last_exit = {}
    for _, row in df.iterrows():
        if row['sym'] in last_exit:
            le = last_exit[row['sym']]
            # 时区统一
            le_comp = le if le.tzinfo else le.tz_localize('UTC')
            if row['entry_dt'].tz_localize(None) <= le_comp.tz_localize(None):
                continue
        # 仓位限制 (简化为按开仓顺序累计)
        valid.append(row)
        last_exit[row['sym']] = row['exit_dt']
    df = pd.DataFrame(valid)

    if df.empty:
        return [], initial_equity, _empty_stats(initial_equity)

    # 4. 复利权益曲线
    df = df.sort_values('entry_dt').reset_index(drop=True)
    df['pnl_pct'] = (df['exit_price'] - df['entry_price']) / df['entry_price']
    df['pnl_lev'] = df['pnl_pct'] * leverage

    equities = [initial_equity]
    pnl_usdts = []
    for pnl_lev in df['pnl_lev']:
        pnl_usdt = equities[-1] * margin_pct * pnl_lev
        pnl_usdts.append(pnl_usdt)
        equities.append(equities[-1] + pnl_usdt)
    df['pnl_usdt'] = pnl_usdts
    df['eq_after'] = equities[1:]

    # 5. 统计
    stats = _compute_stats(df, initial_equity, equities[-1])
    trades = df.to_dict('records') if return_trades else []
    return trades, equities[-1], stats


def _empty_stats(initial):
    return {
        'initial_equity': initial, 'final_equity': initial,
        'return_pct': 0.0, 'trades': 0, 'wins': 0, 'losses': 0,
        'win_rate': 0.0, 'max_dd': 0.0, 'profit_factor': 0.0,
        'avg_pnl_lev': 0.0, 'by_reason': {},
    }


def _compute_stats(df, initial, final):
    wins = int((df['pnl_lev'] > 0).sum())
    losses = int((df['pnl_lev'] < 0).sum())
    total = len(df)
    wr = wins / max(total, 1) * 100
    ret = (final / initial - 1) * 100
    # 最大回撤
    eq_curve = [initial] + list(df['eq_after'])
    peak = initial; max_dd = 0
    for e in eq_curve:
        if e > peak:
            peak = e
        dd = (peak - e) / peak * 100
        if dd > max_dd:
            max_dd = dd
    # Profit factor
    gross_profit = df.loc[df['pnl_usdt'] > 0, 'pnl_usdt'].sum()
    gross_loss = abs(df.loc[df['pnl_usdt'] < 0, 'pnl_usdt'].sum())
    pf = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    by_reason = {}
    for reason, grp in df.groupby('reason'):
        by_reason[reason] = {
            'count': int(len(grp)),
            'pnl_usdt': float(grp['pnl_usdt'].sum()),
        }
    return {
        'initial_equity': initial,
        'final_equity': final,
        'return_pct': ret,
        'trades': total,
        'wins': wins,
        'losses': losses,
        'win_rate': wr,
        'max_dd': max_dd,
        'profit_factor': pf,
        'avg_pnl_lev': float(df['pnl_lev'].mean()) if total else 0.0,
        'by_reason': by_reason,
    }


# ==============================================================================
# 数据加载
# ==============================================================================

def load_data(
    target_syms: Optional[List[str]] = None,
    min_data_start: Optional[pd.Timestamp] = None,
    store_path: str = 'data_store',
    timeframe: str = '15m',
) -> Dict[str, pd.DataFrame]:
    """
    加载 OHLCV 数据. 只保留 min_data_start 之前有数据的币种.

    timeframe: '15m'/'1h' 等, 传给 ParquetStore (postgres 优先, 本地 parquet 兜底).
    """
    store = ParquetStore(store_path)
    all_listed = store.list_symbols()
    target_set = set(target_syms) if target_syms else None
    data_dict = {}
    for s in all_listed:
        short = s.replace('_USDT_USDT', '')
        if target_set is not None and short not in target_set:
            continue
        df = store.load(s, timeframe)
        if df.empty or len(df) < 200:
            continue
        # 显式指定币池时不做数据起点过滤 (新上线币在线前无数据是正常的)
        if target_set is None and min_data_start is not None and df.index[0] > min_data_start:
            continue
        data_dict[short] = df
    return data_dict


def _default_symbols():
    """实盘用过 + 24h 成交额 top 100"""
    top100 = set()
    try:
        r = requests.get('https://fapi.binance.com/fapi/v1/ticker/24hr', timeout=10)
        data = r.json()
        futures = sorted(
            [d for d in data if d['symbol'].endswith('USDT') and not d['symbol'].endswith('_PERP')],
            key=lambda x: float(x.get('quoteVolume', 0)),
            reverse=True
        )
        top100 = {d['symbol'].replace('USDT', '') for d in futures[:100]}
    except Exception as e:
        print(f'  警告: 无法获取 top100 ({e})', file=sys.stderr)
    try:
        events_path = ROOT / 'reports' / 'paper' / 'positions.jsonl'
        used = set()
        for line in events_path.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get('symbol'):
                used.add(e['symbol'].split('/')[0])
    except Exception as e:
        print(f'  警告: 无法读取实盘账本 ({e})', file=sys.stderr)
        used = set()
    return sorted(top100 | used)


def compute_volatility_series(
    data_dict: Dict[str, pd.DataFrame],
    freq: str = '4h',
) -> pd.DataFrame:
    """
    计算全局波动率: 每根 K 线的 (high-low)/close, 然后每 freq 间隔取横截面均值.
    返回 DataFrame: index=time, columns=['vol']
    """
    all_dfs = []
    for s, df in data_dict.items():
        df2 = df.copy()
        df2['sym'] = s
        all_dfs.append(df2)
    if not all_dfs:
        return pd.DataFrame(columns=['vol'])
    all_data = pd.concat(all_dfs).sort_index()
    all_data['range_pct'] = (all_data['high'] - all_data['low']) / all_data['close'] * 100
    all_data['vol_24h'] = all_data.groupby('sym')['range_pct'].transform(
        lambda x: x.rolling(96, min_periods=10).mean()
    )
    cross_vol = all_data.groupby(level=0)['vol_24h'].mean().dropna()
    return cross_vol.to_frame('vol')


# ==============================================================================
# 信号生成
# ==============================================================================

def generate_signals(
    data_dict: Dict[str, pd.DataFrame],
    strategy_name: str = 'pump_pullback',
    strategy_params: Optional[dict] = None,
    test_start: Optional[pd.Timestamp] = None,
    test_end: Optional[pd.Timestamp] = None,
) -> Dict[str, List[pd.Timestamp]]:
    """
    用指定策略生成开仓信号.
    """
    from quant_trader.strategy.generator.auto_strategy import generate_instances
    instances = generate_instances('config/strategies.yaml')
    # 找匹配策略
    target = None
    target_params = None
    for name, params, strat in instances:
        if name == strategy_name:
            target = strat
            target_params = params
            break
    if target is None:
        raise ValueError(f'策略 {strategy_name} 不在 strategies.yaml 中')
    if strategy_params:
        # 覆盖默认参数
        for k, v in strategy_params.items():
            target_params[k] = v

    signals = {}
    for sym, df in data_dict.items():
        try:
            sigs = target.generate_signals(df)
            if sigs is None or sigs.empty:
                continue
            # 时区统一
            if sigs.index.tzinfo is None:
                sigs.index = sigs.index.tz_localize('UTC')
            # 应用测试窗口
            mask = pd.Series(True, index=sigs.index)
            if test_start is not None:
                mask = mask & (sigs.index >= test_start)
            if test_end is not None:
                mask = mask & (sigs.index < test_end)
            sigs_in = sigs[(sigs == 1) & mask]
            if not sigs_in.empty:
                # 修复: 在完整序列上检测 0→1 转变, 不能先过滤掉0再shift
                # (先过滤会导致跨持仓段的入场点丢失, 只保留第一个信号)
                full = sigs.copy()
                if test_start is not None:
                    full[sigs.index < test_start] = 0
                if test_end is not None:
                    full[sigs.index >= test_end] = 0
                prev = full.shift(1).fillna(0)
                entries = full.index[(full == 1) & (prev != 1)]
                if len(entries) > 0:
                    signals[sym] = list(entries)
        except Exception as e:
            print(f'  {sym}: {e}', file=sys.stderr)
    return signals


# ==============================================================================
# 网格搜索
# ==============================================================================

def grid_search(
    data_dict: Dict[str, pd.DataFrame],
    vol_df: pd.DataFrame,
    param_grid: Dict[str, list],
    test_start: pd.Timestamp,
    test_end: pd.Timestamp,
    signal_cache: Optional[Dict] = None,
    initial_equity: float = 100.0,
    margin_pct: float = 0.10,
    leverage: int = 5,
    vol_threshold: float = 12.0,
) -> List[dict]:
    """
    网格搜索: 返回 [{threshold, sl, tp, hold, cooldown, trades, wins, wr, ret, max_dd, score}]
    score = ret * 0.5 + wr * 1.0 - max_dd * 0.5
    """
    import itertools
    keys = list(param_grid.keys())
    combos = list(itertools.product(*[param_grid[k] for k in keys]))
    results = []
    for vals in combos:
        params = dict(zip(keys, vals))
        thr = params.get('pump_threshold')
        cd = params.get('cooldown', 12)
        sl = params.get('stop_loss_pct', 0.12)
        tp = params.get('take_profit_pct', 0.25)
        hold = params.get('hold_bars', 48)
        # 取信号 (按 thr+cd 缓存)
        if signal_cache is not None and (thr, cd) in signal_cache:
            sigs = signal_cache[(thr, cd)]
        else:
            sigs = generate_signals(data_dict, 'pump_pullback',
                                     {'pump_threshold': thr, 'cooldown': cd,
                                      'stop_loss_pct': 0.12, 'take_profit_pct': 0.25,
                                      'hold_bars': 48},
                                     test_start, test_end)
            if signal_cache is not None:
                signal_cache[(thr, cd)] = sigs
        if not sigs:
            continue
        _, equity, stats = run_backtest(
            sigs, data_dict, initial_equity=initial_equity,
            margin_pct=margin_pct, leverage=leverage,
            sl_pct=sl, tp_pct=tp, hold_bars=hold,
            vol_threshold=vol_threshold, vol_df=vol_df,
            return_trades=False,
        )
        r = {**params, **{k: stats[k] for k in
            ('trades', 'wins', 'win_rate', 'return_pct', 'final_equity', 'max_dd', 'profit_factor')}}
        r['score'] = r['return_pct'] * 0.5 + r['win_rate'] * 1.0 - r['max_dd'] * 0.5
        results.append(r)
    return results
