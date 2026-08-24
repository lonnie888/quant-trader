"""
最小化回测器 - 一个币种、一个月，逻辑可逐步验证。
目标：
1. 行为可预测（同一份输入，每次结果完全一致）
2. 单元测试覆盖核心逻辑
3. 持仓检查 + SL/TP 正确
"""
import sys
import json
import pandas as pd
import numpy as np
sys.path.insert(0, '/vol1/1000/quant_trader')


class Position:
    """单笔持仓（不可变快照）"""
    __slots__ = ('sym', 'entry_dt', 'entry', 'sl', 'tp', 'eq_at_entry', 'hold_bars')

    def __init__(self, sym, entry_dt, entry, sl, tp, eq_at_entry, hold_bars):
        self.sym = sym
        self.entry_dt = entry_dt
        self.entry = entry
        self.sl = sl
        self.tp = tp
        self.eq_at_entry = eq_at_entry
        self.hold_bars = hold_bars

    def to_dict(self):
        return {
            'sym': self.sym, 'entry_dt': self.entry_dt,
            'entry': self.entry, 'sl': self.sl, 'tp': self.tp,
            'eq_at_entry': self.eq_at_entry, 'hold_bars': self.hold_bars,
        }


def check_sl_tp(pos: Position, future_df: pd.DataFrame):
    """
    检查持仓在 future K 线序列里是否触发 SL/TP。
    返回 (exit_dt, exit_price, reason) 或 None。
    规则：每根 K 线按 OHLC 顺序，先看低后看高（保守：同日内先 SL 后 TP）。
    """
    for i in range(len(future_df)):
        row = future_df.iloc[i]
        dt = future_df.index[i]
        lo = float(row['low'])
        hi = float(row['high'])
        # 同 K 线内先检查 SL（保守）
        if lo <= pos.sl:
            return dt, pos.sl, 'SL'
        if hi >= pos.tp:
            return dt, pos.tp, 'TP'
    return None


def check_time_exit(pos: Position, current_dt):
    """检查是否到期（hold_bars 根 K 线已过）。"""
    # 计算从 entry_dt 到 current_dt 之间有几根 K 线
    # 简化：current_dt > entry_dt + hold_bars * 15min
    elapsed = (current_dt - pos.entry_dt).total_seconds() / 60
    if elapsed >= pos.hold_bars * 15:
        return True
    return False


def run_simple_backtest(
    signals: dict,  # sym -> sorted list of entry timestamps
    data_dict: dict,  # sym -> OHLCV DataFrame
    initial_equity: float = 100.0,
    margin_pct: float = 0.10,
    leverage: int = 5,
    sl_pct: float = 0.12,
    tp_pct: float = 0.25,
    hold_bars: int = 48,
    use_weekend_filter: bool = True,
    use_bad_hours_filter: bool = True,
    bad_hours: set = None,
    use_volatility_filter: bool = False,
    vol_threshold: float = 12.0,
    vol_series: dict = None,  # time -> volatility
    max_concurrent: int = 20,
):
    """
    最小化回测。规则：
    - 同一时间可能触发多个信号，按时间顺序处理
    - 持仓检查：同币种已有 open 时不开新仓
    - 仓位限制：同时最多 max_concurrent 个
    - 复利：每笔盈亏 = 当时权益 × margin × leverage × pnl_pct

    Returns: (trades_list, final_equity)
    """
    if bad_hours is None:
        bad_hours = {0, 1, 2, 3, 16, 17, 18, 19}

    # 收集所有事件时间点：信号时间 + 所有持仓期间的 K 线时间
    # 关键修复：必须遍历所有 K 线才能正确检查 SL/TP
    signal_times = set()
    for sym, ts_list in signals.items():
        for t in ts_list:
            signal_times.add(t)

    # 所有 K 线时间（用于检查 SL/TP）
    all_klines = set()
    for sym, df in data_dict.items():
        # 只取信号时间之后、以及信号时间之前的 K 线（用于正确检查）
        all_klines.update(df.index.tolist())

    # 事件时间 = 信号时间 ∪ K线时间（限制在信号区间 + 最大持有期后）
    min_t = min(signal_times) if signal_times else None
    max_signal_t = max(signal_times) if signal_times else None
    if min_t is None:
        return [], initial_equity
    # 最大持有期结束时间：最后一笔开仓 + hold_bars 根 K 线
    max_hold_end = max_signal_t + pd.Timedelta(minutes=hold_bars * 15)
    # 保留 [min_t, max_hold_end] 之间的 K 线
    all_klines = {t for t in all_klines if min_t <= t <= max_hold_end}
    all_events = sorted(signal_times | all_klines)

    trades = []
    open_positions = {}  # sym -> Position
    equity = initial_equity

    for t in all_events:
        # 1. 先检查已有持仓
        to_close = []
        for sym, pos in list(open_positions.items()):
            # 取该持仓在 [entry_dt, t] 之间的 K 线
            future = data_dict[sym][(data_dict[sym].index > pos.entry_dt) & (data_dict[sym].index <= t)]
            if future.empty:
                continue

            # 先检查 SL/TP
            result = check_sl_tp(pos, future)
            if result:
                exit_dt, exit_price, reason = result
                pnl_pct = (exit_price - pos.entry) / pos.entry
                pnl_lev = pnl_pct * leverage
                pnl_usdt = pos.eq_at_entry * margin_pct * pnl_lev
                equity += pnl_usdt
                trades.append({
                    'sym': sym, 'entry_dt': pos.entry_dt, 'exit_dt': exit_dt,
                    'reason': reason, 'pnl_pct': pnl_pct, 'pnl_lev': pnl_lev,
                    'pnl_usdt': pnl_usdt, 'eq_after': equity,
                })
                to_close.append(sym)
            else:
                # 检查是否到期
                if check_time_exit(pos, t):
                    # 按当前价平仓
                    last_price = float(future.iloc[-1]['close'])
                    pnl_pct = (last_price - pos.entry) / pos.entry
                    pnl_lev = pnl_pct * leverage
                    pnl_usdt = pos.eq_at_entry * margin_pct * pnl_lev
                    equity += pnl_usdt
                    trades.append({
                        'sym': sym, 'entry_dt': pos.entry_dt, 'exit_dt': t,
                        'reason': 'time', 'pnl_pct': pnl_pct, 'pnl_lev': pnl_lev,
                        'pnl_usdt': pnl_usdt, 'eq_after': equity,
                    })
                    to_close.append(sym)

        for s in to_close:
            del open_positions[s]

        # 2. 检查这个时间点哪些币种触发信号
        for sym, ts_list in signals.items():
            if t not in ts_list:
                continue
            if sym in open_positions:
                continue  # 持仓检查
            if len(open_positions) >= max_concurrent:
                continue  # 仓位限制

            # 过滤器
            if use_weekend_filter and t.weekday() >= 5:
                continue
            if use_bad_hours_filter and t.hour in bad_hours:
                continue
            if use_volatility_filter and vol_series is not None:
                vol = vol_series.get(t, 0)
                if vol > vol_threshold:
                    continue

            # 开仓
            df = data_dict[sym]
            if t not in df.index:
                continue
            row = df.loc[t]
            entry = float(row['close'])
            if entry <= 0:
                continue
            sl = entry * (1 - sl_pct)
            tp = entry * (1 + tp_pct)
            pos = Position(
                sym=sym, entry_dt=t, entry=entry, sl=sl, tp=tp,
                eq_at_entry=equity, hold_bars=hold_bars,
            )
            open_positions[sym] = pos
            trades.append({
                'sym': sym, 'entry_dt': t, 'exit_dt': None,
                'reason': 'open', 'pnl_pct': None, 'pnl_lev': None,
                'pnl_usdt': None, 'eq_after': equity,
            })

    # 3. 最后平仓所有未平仓位
    final_t = max(t for ts_list in signals.values() for t in ts_list)
    for sym, pos in list(open_positions.items()):
        df = data_dict[sym]
        future = df[df.index >= pos.entry_dt]
        if not future.empty:
            last = float(future.iloc[-1]['close'])
            pnl_pct = (last - pos.entry) / pos.entry
            pnl_lev = pnl_pct * leverage
            pnl_usdt = pos.eq_at_entry * margin_pct * pnl_lev
            equity += pnl_usdt
            trades.append({
                'sym': sym, 'entry_dt': pos.entry_dt, 'exit_dt': final_t,
                'reason': 'end', 'pnl_pct': pnl_pct, 'pnl_lev': pnl_lev,
                'pnl_usdt': pnl_usdt, 'eq_after': equity,
            })

    return trades, equity


if __name__ == '__main__':
    # 简单测试：单个币种 1 天
    import datetime as dt
    times = pd.date_range('2026-01-15 12:00', periods=8, freq='15min', tz='UTC')
    df = pd.DataFrame({
        'open': [100, 101, 102, 103, 102, 101, 100, 99],
        'high': [100, 102, 103, 105, 103, 102, 101, 100],
        'low':  [100, 100, 101, 102, 101, 100, 99, 98],
        'close':[100, 101, 102, 104, 102, 101, 100, 99],
        'volume': [100] * 8,
    }, index=times)
    data_dict = {'TEST': df}
    signals = {'TEST': [times[0]]}  # 第 1 根 K 线开仓

    trades, equity = run_simple_backtest(signals, data_dict)
    print('=== 单元测试 1: 简单上涨 ===')
    print(f'  交易数: {len([t for t in trades if t.get("reason") != "open"])}')
    print(f'  最终权益: {equity:.2f} (初始 100)')
    for t in trades:
        print(f'  {t["reason"]:<6} entry={t.get("entry_dt")} exit={t.get("exit_dt")} pnl_lev={t.get("pnl_lev")} pnl_usdt={t.get("pnl_usdt")}')

    # 测试 2: 立即 SL
    times2 = pd.date_range('2026-01-15 12:00', periods=4, freq='15min', tz='UTC')
    df2 = pd.DataFrame({
        'open': [100, 99, 95, 90],
        'high': [100, 100, 96, 91],
        'low':  [100, 90, 88, 89],   # 90 触发 SL (100*0.88=88, 但 90 > 88, 不触发!)
        'close':[100, 91, 89, 90],
        'volume': [100] * 4,
    }, index=times2)
    data_dict2 = {'TEST': df2}
    signals2 = {'TEST': [times2[0]]}

    trades2, equity2 = run_simple_backtest(signals2, data_dict2)
    print('\n=== 单元测试 2: 下跌 (低=90, 100*0.88=88 → 不触发 SL, time 退出) ===')
    print(f'  最终权益: {equity2:.2f}')
    for t in trades2:
        print(f'  {t["reason"]:<6} pnl_lev={t.get("pnl_lev")}')

    # 测试 3: 立即 SL (低=87, 100*0.88=88 → 触发)
    times3 = pd.date_range('2026-01-15 12:00', periods=3, freq='15min', tz='UTC')
    df3 = pd.DataFrame({
        'open': [100, 99, 95],
        'high': [100, 100, 96],
        'low':  [100, 87, 88],   # 第 2 根 87 < SL 88
        'close':[100, 90, 89],
        'volume': [100] * 3,
    }, index=times3)
    data_dict3 = {'TEST': df3}
    signals3 = {'TEST': [times3[0]]}

    trades3, equity3 = run_simple_backtest(signals3, data_dict3)
    print('\n=== 单元测试 3: 立即 SL (低=87 < 88) ===')
    print(f'  最终权益: {equity3:.2f} (应=100-0.6*0.1*100=94)')
    for t in trades3:
        print(f'  {t["reason"]:<6} pnl_lev={t.get("pnl_lev")}')

    # 测试 4: 持仓检查（同币种二次开仓应被过滤）
    times4 = pd.date_range('2026-01-15 12:00', periods=4, freq='15min', tz='UTC')
    df4 = pd.DataFrame({
        'open': [100, 100, 100, 100],
        'high': [100, 110, 110, 110],  # 多次触发 TP
        'low':  [100, 100, 100, 100],
        'close':[100, 105, 105, 105],
        'volume': [100] * 4,
    }, index=times4)
    data_dict4 = {'TEST': df4}
    signals4 = {'TEST': [times4[0], times4[1], times4[2]]}  # 3 个信号
    trades4, equity4 = run_simple_backtest(signals4, data_dict4)
    print('\n=== 单元测试 4: 同币种多次信号 (持仓检查) ===')
    print(f'  交易: {len([t for t in trades4 if t.get("reason") != "open"])}')
    for t in trades4:
        if t.get('reason') in ('open', 'TP', 'SL', 'time', 'end'):
            print(f'  {t["reason"]:<6} sym={t["sym"]} pnl_lev={t.get("pnl_lev")}')
