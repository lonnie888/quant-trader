"""
回测 CLI 工具 - 不需要 AI 就能跑回测.

用法:
    # 单次回测
    python tests/cli.py backtest --start 2026-01-01 --end 2026-08-18

    # 网格搜索
    python tests/cli.py grid --output /tmp/grid.json

    # 单元测试
    python tests/cli.py test

    # 跑实盘
    python tests/cli.py analyze /vol1/1000/quant_trader/reports/paper/positions.jsonl
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))

import pandas as pd
import numpy as np

from backtest import (
    load_data, compute_volatility_series, generate_signals,
    run_backtest, grid_search,
)


def cmd_backtest(args):
    """单次回测 - 跑 strategies.yaml 当前参数"""
    print('加载数据...')
    t0 = time.time()
    test_start = pd.Timestamp(args.start, tz='UTC')
    test_end = pd.Timestamp(args.end, tz='UTC')
    target = args.symbols.split(',') if args.symbols else None
    data_dict = load_data(min_data_start=test_start, target_syms=target, timeframe=args.tf)
    print(f'  币种: {len(data_dict)} ({time.time()-t0:.0f}s, tf={args.tf})')

    print('计算波动率...')
    vol_df = compute_volatility_series(data_dict)
    print(f'  快照: {len(vol_df)}')

    print('生成信号...')
    sigs = generate_signals(data_dict, args.strategy, test_start=test_start, test_end=test_end)
    n_signals = sum(len(v) for v in sigs.values())
    print(f'  信号: {n_signals} (n_sym={len(sigs)})')

    print('回测...')
    trades, equity, stats = run_backtest(
        sigs, data_dict,
        initial_equity=args.initial,
        sl_pct=args.sl, tp_pct=args.tp, hold_bars=args.hold,
        vol_threshold=args.vol_th, vol_df=vol_df,
    )
    print()
    print('=== 回测结果 ===')
    print(f'初始资金:    {stats["initial_equity"]:.2f} USDT')
    print(f'最终权益:    {stats["final_equity"]:.2f} USDT')
    print(f'总收益:      {stats["return_pct"]:+.2f}%')
    print(f'交易数:      {stats["trades"]}')
    print(f'胜 / 负:     {stats["wins"]} / {stats["losses"]}')
    print(f'胜率:        {stats["win_rate"]:.1f}%')
    print(f'最大回撤:    {stats["max_dd"]:.1f}%')
    print(f'盈亏比:      {stats["profit_factor"]:.2f}')
    print()
    print('=== 平仓原因 ===')
    for reason, info in stats['by_reason'].items():
        print(f'  {reason:10s}: {info["count"]:>3}笔  累计 {info["pnl_usdt"]:+.2f} USDT')

    if args.output:
        # 保存
        out = {
            'config': vars(args),
            'stats': {k: v for k, v in stats.items() if k != 'by_reason'},
            'by_reason': stats['by_reason'],
            'n_signals': n_signals,
        }
        with open(args.output, 'w') as f:
            json.dump(out, f, indent=2, default=str)
        print(f'\n报告保存: {args.output}')


def cmd_grid(args):
    """网格搜索"""
    from backtest import grid_search
    import itertools
    print('加载数据...')
    t0 = time.time()
    test_start = pd.Timestamp(args.start, tz='UTC')
    test_end = pd.Timestamp(args.end, tz='UTC')
    data_dict = load_data(min_data_start=test_start)
    print(f'  币种: {len(data_dict)} ({time.time()-t0:.0f}s)')

    print('计算波动率...')
    vol_df = compute_volatility_series(data_dict)

    # 默认网格
    param_grid = {
        'pump_threshold': [0.10, 0.13, 0.16, 0.20],
        'stop_loss_pct': [0.08, 0.12, 0.15],
        'take_profit_pct': [0.25, 0.35, 0.50],
        'hold_bars': [24, 48, 72],
        'cooldown': [12, 24],
    }
    n_combos = 1
    for v in param_grid.values():
        n_combos *= len(v)
    print(f'网格: {n_combos} 组合')

    print('跑网格...')
    t0 = time.time()
    sig_cache = {}
    results = grid_search(
        data_dict, vol_df, param_grid,
        test_start, test_end,
        signal_cache=sig_cache,
        vol_threshold=args.vol_th,
    )
    print(f'  耗时: {time.time()-t0:.0f}s')

    if not results:
        print('无结果')
        return

    # 排序
    results.sort(key=lambda x: -x['score'])
    print()
    print('=== Top 20 (综合得分) ===')
    print(f'{"thr":<5} {"sl":<5} {"tp":<5} {"hold":<5} {"cd":<3} {"#":<4} {"胜率":<7} {"收益":<10} {"回撤":<7} {"得分":<7}')
    for r in results[:20]:
        print(f"{r['pump_threshold']:<5} {r['stop_loss_pct']:<5} {r['take_profit_pct']:<5} "
              f"{r['hold_bars']:<5} {r['cooldown']:<3} {r['trades']:<4} "
              f"{r['win_rate']:<7.1f} {r['return_pct']:+8.2f}% {r['max_dd']:<7.1f} {r['score']:<7.1f}")

    if args.output:
        with open(args.output, 'w') as f:
            json.dump(results, f, indent=2, default=str)
        print(f'\n保存: {args.output} ({len(results)} 组)')


def cmd_test(args):
    """单元测试"""
    import subprocess
    import importlib
    # 直接 import + 调用 __main__ 部分
    sys.path.insert(0, str(ROOT / 'tests'))
    import test_backtest
    print('=== 单元测试 ===')
    # 复用 test_backtest 的 __main__ 逻辑
    test_backtest_globals = test_backtest
    import datetime as dt
    # 重跑测试 1
    times = pd.date_range('2026-01-15 12:00', periods=8, freq='15min', tz='UTC')
    df = pd.DataFrame({
        'open': [100, 101, 102, 103, 102, 101, 100, 99],
        'high': [100, 102, 103, 105, 103, 102, 101, 100],
        'low':  [100, 100, 101, 102, 101, 100, 99, 98],
        'close':[100, 101, 102, 104, 102, 101, 100, 99],
        'volume': [100] * 8,
    }, index=times)
    trades, equity = test_backtest_globals.run_simple_backtest({'TEST': [times[0]]}, {'TEST': df})
    assert abs(equity - 99.50) < 0.01, f'测试1 失败: equity={equity}'
    print('  测试1 简单上涨 ✅')

    # 测试3 立即 SL
    times3 = pd.date_range('2026-01-15 12:00', periods=3, freq='15min', tz='UTC')
    df3 = pd.DataFrame({
        'open': [100, 99, 95],
        'high': [100, 100, 96],
        'low':  [100, 87, 88],
        'close':[100, 90, 89],
        'volume': [100] * 3,
    }, index=times3)
    trades3, equity3 = test_backtest_globals.run_simple_backtest({'TEST': [times3[0]]}, {'TEST': df3})
    assert abs(equity3 - 94.0) < 0.01, f'测试3 失败: equity={equity3}'
    print('  测试3 立即 SL ✅')

    # 测试4 持仓检查
    times4 = pd.date_range('2026-01-15 12:00', periods=4, freq='15min', tz='UTC')
    df4 = pd.DataFrame({
        'open': [100, 100, 100, 100],
        'high': [100, 110, 110, 110],
        'low':  [100, 100, 100, 100],
        'close':[100, 105, 105, 105],
        'volume': [100] * 4,
    }, index=times4)
    trades4, equity4 = test_backtest_globals.run_simple_backtest(
        {'TEST': [times4[0], times4[1], times4[2]]}, {'TEST': df4})
    closed = [t for t in trades4 if t.get('reason') not in ('open',)]
    assert len(closed) == 1, f'测试4 失败: closed={len(closed)}'
    print('  测试4 持仓检查 ✅')

    print('\n=== 全部通过 ===')


def cmd_analyze(args):
    """分析实盘账本 (positions.jsonl) - 跟之前 /tmp/analyze_paper.py 一样"""
    from collections import defaultdict
    path = Path(args.path)
    if not path.exists():
        print(f'文件不存在: {path}')
        return
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    opens = [e for e in lines if e.get('status') == 'open']
    closeds = [e for e in lines if e.get('status') == 'closed']
    print(f'=== 模拟盘分析 ({path}) ===')
    print(f'总事件: {len(lines)}, open: {len(opens)}, closed: {len(closeds)}')
    if not closeds:
        return
    total_pnl_lev = sum(e.get('pnl_pct_lev', 0) or 0 for e in closeds)
    wins = sum(1 for e in closeds if (e.get('pnl_pct_lev', 0) or 0) > 0)
    losses = sum(1 for e in closeds if (e.get('pnl_pct_lev', 0) or 0) < 0)
    print(f'胜率: {wins}/{len(closeds)} = {wins/max(len(closeds),1)*100:.1f}%')
    print(f'杠杆收益加总: {total_pnl_lev*100:+.2f}%')
    # 复利
    equity = 100.0
    MARGIN = 0.10
    LEV = 5
    for e in sorted(closeds, key=lambda x: x.get('exit_ts', '')):
        pnl_lev = e.get('pnl_pct_lev', 0) or 0
        equity += equity * MARGIN * pnl_lev
    print(f'复利: 100 → {equity:.2f} ({(equity/100-1)*100:+.2f}%)')

    # 按日
    by_day = defaultdict(lambda: {'trades':0, 'wins':0, 'pnl_lev':0.0})
    for e in closeds:
        day = (e.get('exit_ts','') or e.get('entry_ts',''))[:10]
        pnl = e.get('pnl_pct_lev', 0) or 0
        by_day[day]['trades'] += 1
        by_day[day]['pnl_lev'] += pnl
        if pnl > 0:
            by_day[day]['wins'] += 1
    print('\n按日:')
    for d in sorted(by_day.keys()):
        v = by_day[d]
        negs = v['trades'] - v['wins']
        wr = v['wins']/max(v['trades'],1)*100
        bar = '+' if v['pnl_lev'] > 0 else '-'
        print(f'  {d}: {v["trades"]}笔 {v["wins"]}胜{negs}负 {wr:.0f}% 收益{v["pnl_lev"]*100:+.1f}% {bar}')

    # 平仓原因
    by_reason = defaultdict(int)
    for e in closeds:
        by_reason[e.get('exit_reason','')] += 1
    print('\n平仓原因:')
    for r, c in sorted(by_reason.items(), key=lambda x: -x[1]):
        pnl_sum = sum(e.get('pnl_pct_lev',0) or 0 for e in closeds if e.get('exit_reason') == r)
        print(f'  {r:10s}: {c}笔, 累计 {pnl_sum*100:+.1f}%')


def main():
    parser = argparse.ArgumentParser(description='Quant Trader 回测 CLI')
    subparsers = parser.add_subparsers(dest='cmd', required=True)

    # backtest
    p = subparsers.add_parser('backtest', help='单次回测')
    p.add_argument('--start', default='2026-01-01', help='开始日期 YYYY-MM-DD')
    p.add_argument('--end', default='2026-08-18', help='结束日期 YYYY-MM-DD')
    p.add_argument('--strategy', default='pump_pullback', help='策略名 (如 pump_pullback_opt)')
    p.add_argument('--tf', default='15m', help='K 线周期 15m/1h (postgres 自动聚合)')
    p.add_argument('--symbols', default='', help='目标币池, 逗号分隔 (空=全部)')
    p.add_argument('--initial', type=float, default=100.0, help='初始资金 USDT')
    p.add_argument('--sl', type=float, default=0.12, help='止损比例')
    p.add_argument('--tp', type=float, default=0.25, help='止盈比例')
    p.add_argument('--hold', type=int, default=48, help='持有 K 线数')
    p.add_argument('--vol-th', type=float, default=12.0, help='波动率阈值')
    p.add_argument('--output', help='报告输出 JSON 路径')
    p.set_defaults(func=cmd_backtest)

    # grid
    p = subparsers.add_parser('grid', help='网格搜索')
    p.add_argument('--start', default='2026-01-01')
    p.add_argument('--end', default='2026-08-18')
    p.add_argument('--vol-th', type=float, default=12.0)
    p.add_argument('--output', default='/tmp/grid.json')
    p.set_defaults(func=cmd_grid)

    # test
    p = subparsers.add_parser('test', help='单元测试')
    p.set_defaults(func=cmd_test)

    # analyze
    p = subparsers.add_parser('analyze', help='分析实盘账本')
    p.add_argument('path', nargs='?', default='reports/paper/positions.jsonl')
    p.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
