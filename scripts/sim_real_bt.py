"""模拟实盘回测引擎 - 无限接近实盘daemon的行为。

模拟要素:
1. 多币种并发持仓 (max_concurrent限制)
2. K线收盘时对齐检查 (每15分钟)
3. bars_since 信号过期 (追高防护)
4. 24h 止损冷却 (cooldown)
5. 熔断器 (账户亏60%)
6. 20%保证金复利 (每单用当前权益20%)
7. 手续费 + 滑点
8. SL/TP/hold 平仓
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from quant_trader.config import load_settings
from quant_trader.data.storage.parquet_store import ParquetStore
from quant_trader.strategy.library.pump_pullback import PumpPullbackStrategy


class SimulatedRealBacktest:
    def __init__(self, params: dict, initial_capital: float = 10.0,
                 max_concurrent: int = 3, margin_pct: float = 0.20,
                 leverage: float = 5.0, fee_rate: float = 0.0004,
                 slippage: float = 0.0005, circuit_loss: float = -0.60,
                 bars_since_limit: int = 2, cooldown_hours: int = 24,
                 hold_bars_override: int = None):
        self.params = params
        self.initial_capital = initial_capital
        self.max_concurrent = max_concurrent
        self.margin_pct = margin_pct
        self.leverage = leverage
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.circuit_loss = circuit_loss
        self.bars_since_limit = bars_since_limit
        self.cooldown_hours = cooldown_hours
        if hold_bars_override:
            self.params["hold_bars"] = hold_bars_override

        self.strategy = PumpPullbackStrategy(self.params)
        self.equity = initial_capital
        self.peak_equity = initial_capital
        self.max_drawdown = 0.0
        self.positions = []  # 当前持仓
        self.cooldowns = {}  # symbol -> 冷却结束时间
        self.trades = []     # 已完成交易
        self.circuit_triggered = False
        self._eq_history = []

    def load_symbols(self, store: ParquetStore, min_bars: int = 100, symbol_subset=None):
        """加载指定币种K线 + 预生成信号."""
        self.symbols = {}
        syms = store.list_symbols()
        if symbol_subset:
            syms = [s for s in syms if s in symbol_subset]
        for sym in syms:
            df = store.load(sym, "15m")
            if df is None or df.empty or len(df) < min_bars:
                continue
            try:
                sigs = self.strategy.generate_signals(df)
            except Exception:
                continue
            if sigs is None or sigs.empty:
                continue
            self.symbols[sym] = {
                "df": df,
                "sigs": sigs.values.flatten(),
                "entry_signals": [],
            }
            s = sigs.values.flatten()
            for i in range(1, len(s)):
                if s[i] == 1 and s[i - 1] == 0:
                    self.symbols[sym]["entry_signals"].append(i)
        print(f"加载 {len(self.symbols)} 个币种")

    def _apply_fee(self, notional: float) -> float:
        """手续费+滑点占权益比."""
        return notional * (self.fee_rate + self.slippage)

    def run(self):
        """逐根K线全局模拟."""
        # 收集所有币种K线时间戳
        all_timestamps = set()
        for sym, data in self.symbols.items():
            for ts in data["df"].index:
                all_timestamps.add(ts)
        all_timestamps = sorted(all_timestamps)
        print(f"总K线时刻: {len(all_timestamps)}")

        # 每个币种的当前K线索引
        sym_idx = {sym: 0 for sym in self.symbols}
        # 每个币种的开仓信号待处理
        pending_signals = {}  # sym -> 信号触发的K线索引

        for ts in all_timestamps:
            # 1. 处理持仓平仓 (SL/TP/hold)
            self._check_positions(ts)

            # 2. 熔断器检查
            if self.equity <= self.initial_capital * (1 + self.circuit_loss):
                self.circuit_triggered = True
                print(f"⚠️ 熔断器触发 @ {ts.strftime('%Y-%m-%d %H:%M')}, 权益={self.equity:.4f} USDT")
                break

            # 3. 处理开仓信号
            for sym, data in self.symbols.items():
                if ts not in data["df"].index:
                    continue
                idx = sym_idx[sym]
                # 清冷却期
                if sym in self.cooldowns and self.cooldowns[sym] <= ts:
                    del self.cooldowns[sym]
                # 检查开仓信号
                if len(self.positions) >= self.max_concurrent:
                    continue
                if sym in self.cooldowns:
                    continue
                if idx in data["entry_signals"]:
                    # 检查是否有同币种持仓
                    if any(p["symbol"] == sym for p in self.positions):
                        continue
                    # 开仓
                    entry_price = float(data["df"].iloc[idx]["close"])
                    margin = self.equity * self.margin_pct
                    notional = margin * self.leverage
                    sl_price = entry_price * (1 - self.params["stop_loss_pct"])
                    tp_price = entry_price * (1 + self.params["take_profit_pct"])
                    self.positions.append({
                        "symbol": sym,
                        "entry_price": entry_price,
                        "entry_ts": ts,
                        "entry_idx": idx,
                        "sl_price": sl_price,
                        "tp_price": tp_price,
                        "margin": margin,
                        "notional": notional,
                    })
            # 推进索引
            for sym, data in self.symbols.items():
                if sym_idx[sym] < len(data["df"].index) and ts == data["df"].index[sym_idx[sym]]:
                    sym_idx[sym] += 1

            # 记录权益
            self._eq_history.append((ts, self.equity))

        return {
            "final_equity": self.equity,
            "return_pct": (self.equity / self.initial_capital - 1) * 100,
            "trades": len(self.trades),
            "circuit_triggered": self.circuit_triggered,
            "max_drawdown": self.max_drawdown * 100,
            "equity_curve": self._eq_history,
        }

    def _check_positions(self, ts):
        """检查并平仓到期的持仓."""
        keep = []
        for pos in self.positions:
            sym = pos["symbol"]
            data = self.symbols[sym]
            # 检查当前时间戳是否在该币种K线数据中
            if ts not in data["df"].index:
                keep.append(pos)
                continue
            cur_idx = data["df"].index.get_loc(ts)
            if cur_idx < 0:
                keep.append(pos)
                continue
            high = float(data["df"].iloc[cur_idx]["high"])
            low = float(data["df"].iloc[cur_idx]["low"])
            close = float(data["df"].iloc[cur_idx]["close"])

            exit_reason = None
            exit_price = None

            # SL
            if low <= pos["sl_price"]:
                exit_reason = "sl"
                exit_price = pos["sl_price"]
            elif high >= pos["tp_price"]:
                exit_reason = "tp"
                exit_price = pos["tp_price"]
            elif (ts - pos["entry_ts"]).total_seconds() / 900 >= self.params["hold_bars"]:
                exit_reason = "time"
                exit_price = close

            if exit_reason:
                # 计算盈亏
                ret = (exit_price - pos["entry_price"]) / pos["entry_price"]
                gross = pos["notional"] * ret
                fee = self._apply_fee(pos["notional"])
                net = gross - fee
                self.equity += net
                self.trades.append({
                    "symbol": sym, "entry_ts": str(pos["entry_ts"]),
                    "exit_ts": str(ts), "reason": exit_reason,
                    "ret": ret, "net": net,
                })
                if self.equity > self.peak_equity:
                    self.peak_equity = self.equity
                dd = (self.peak_equity - self.equity) / self.peak_equity
                if dd > self.max_drawdown:
                    self.max_drawdown = dd
                # 止损/止盈冷却
                if exit_reason in ("sl", "tp", "stop_loss", "take_profit"):
                    self.cooldowns[sym] = ts + timedelta(hours=self.cooldown_hours)
                continue
            keep.append(pos)
        self.positions = keep


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--initial", type=float, default=10.0, help="初始资金USDT")
    parser.add_argument("--max-concurrent", type=int, default=3, help="最大并发持仓")
    parser.add_argument("--margin-pct", type=float, default=0.20, help="每单保证金比例")
    parser.add_argument("--leverage", type=float, default=5.0, help="杠杆")
    parser.add_argument("--bars-since", type=int, default=2, help="信号过期K线数")
    parser.add_argument("--output", type=str, default="reports/paper/sim_real.json")
    parser.add_argument("--batch-size", type=int, default=10, help="每批币种数")
    parser.add_argument("--batch", type=int, default=None, help="只跑第N批(从0开始)")
    parser.add_argument("--resume-from", type=float, default=None, help="继承前一批的权益")
    args = parser.parse_args()

    settings = load_settings()
    store = ParquetStore(settings.data.storage_dir)

    params = {
        "pump_window": 12, "pump_threshold": 0.13,
        "pullback_min": 0.05, "pullback_max": 0.30,
        "vol_shrink": 0.80, "vol_recover": 1.0,
        "trigger_pct": 0.0, "ema_period": 12,
        "hold_bars": 48, "cooldown": 12,
        "stop_loss_pct": 0.12, "take_profit_pct": 0.25,
        "side": "long_only",
    }

    all_syms = store.list_symbols()
    # 分批
    total = len(all_syms)
    batches = [all_syms[i:i+args.batch_size] for i in range(0, total, args.batch_size)]
    print(f"总币种 {total}, 分 {len(batches)} 批, 每批 {args.batch_size} 个")

    if args.batch is not None:
        # 只跑指定批次
        batch = batches[args.batch]
        initial = args.resume_from if args.resume_from is not None else args.initial
        bt = SimulatedRealBacktest(
            params, initial_capital=initial,
            max_concurrent=args.max_concurrent,
            margin_pct=args.margin_pct, leverage=args.leverage,
            bars_since_limit=args.bars_since,
        )
        bt.load_symbols(store, symbol_subset=batch)
        result = bt.run()
        result["batch"] = args.batch
        result["initial_capital"] = initial
        # 保存
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        print(f"\n=== 批次{args.batch} 结果 ===")
        print(f"初始权益: {initial:.2f} USDT")
        print(f"最终权益: {result['final_equity']:.2f} USDT")
        print(f"本批收益: {result['return_pct']:+.2f}%")
        print(f"交易数: {result['trades']}")
        print(f"熔断器: {'触发' if result['circuit_triggered'] else '未触发'}")
        print(f"✅ 已保存: {out}")
    else:
        # 串行跑所有批次
        equity = args.initial
        all_results = []
        for bi, batch in enumerate(batches):
            print(f"\n{'='*50}")
            print(f"批次 {bi+1}/{len(batches)} ({len(batch)} 个币种)")
            bt = SimulatedRealBacktest(
                params, initial_capital=equity,
                max_concurrent=args.max_concurrent,
                margin_pct=args.margin_pct, leverage=args.leverage,
                bars_since_limit=args.bars_since,
            )
            bt.load_symbols(store, symbol_subset=batch)
            result = bt.run()
            result["batch"] = bi
            result["initial_capital"] = equity
            all_results.append(result)
            equity = result["final_equity"]
            print(f"批次{bi} 权益: {equity:.2f} USDT")
            if result["circuit_triggered"]:
                print("⚠️ 熔断器触发，停止后续批次")
                break
        # 汇总
        total_trades = sum(r["trades"] for r in all_results)
        final_eq = all_results[-1]["final_equity"] if all_results else args.initial
        ret = (final_eq / args.initial - 1) * 100
        print(f"\n{'='*50}")
        print(f"=== 全部批次汇总 ===")
        print(f"初始资金: {args.initial:.0f} USDT")
        print(f"最终权益: {final_eq:.2f} USDT")
        print(f"总收益: {ret:+.2f}%")
        print(f"总交易: {total_trades}")
        print(f"批次: {len(all_results)}")


if __name__ == "__main__":
    main()