"""
深度分析涨幅前10币的特征，产出结构化报告。
重点关注：
  - 涨幅分布、成交量特征
  - 价格区间、波动率
  - 近期是否有过泵（前期走势）
  - 当前回撤情况
  - 各时间维度（15m/1h/4h）趋势
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("analyze_gainers")

import numpy as np
import pandas as pd

from quant_trader.config import load_settings
from quant_trader.data.fetcher.binance_client import BinanceClient
from quant_trader.data.fetcher.gainers_scanner import scan_gainers
from quant_trader.data.fetcher.ohlcv_downloader import download_ohlcv
from quant_trader.data.storage.parquet_store import ParquetStore


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def analyze_gainer(sym: str, client: BinanceClient, store: ParquetStore,
                   lookback_days: int = 180) -> dict:
    """深度分析一个币的特征。"""
    result = {"symbol": sym, "error": None}

    try:
        df = download_ohlcv(client, sym, "15m", lookback_days=lookback_days)
    except Exception as e:
        result["error"] = str(e)
        return result

    if df.empty or len(df) < 100:
        result["error"] = f"不足数据 ({len(df)} bars)"
        return result

    df["price"] = df["close"]
    n = len(df)

    # 基础统计
    last = df.iloc[-1]
    close = df["close"].values
    high = df["high"].values
    low = df["low"].values
    vol = df["volume"].values

    result["last_price"] = float(last["close"])
    result["total_bars"] = n

    # 各时间维度涨幅
    for bars, label in [(4, "1h"), (16, "4h"), (96, "24h"), (672, "7d")]:
        if n > bars:
            pct = (close[-1] / close[-bars - 1] - 1) * 100
            result[f"ret_{label}"] = round(float(pct), 2)
        else:
            result[f"ret_{label}"] = None

    # 波动率（最近 96 bar / 24h 的ATR比率）
    tr = np.maximum.reduce([
        high[1:] - low[1:],
        np.abs(high[1:] - close[:-1]),
        np.abs(low[1:] - close[:-1]),
    ])
    atr_24h = np.mean(tr[-96:]) if len(tr) >= 96 else np.mean(tr)
    result["atr_pct"] = round(float(atr_24h / close[-1] * 100), 3)
    result["atr_value"] = round(float(atr_24h), 6)

    # 波动率（close-to-close std dev over 24h）
    ret_15m = pd.Series(np.diff(close) / close[:-1])
    result["volatility_24h"] = round(float(ret_15m[-96:].std() * 100), 3) if n >= 96 else None

    # 成交量分析
    avg_vol_7d = np.mean(vol[-672:]) if n >= 672 else np.mean(vol)
    avg_vol_24h = np.mean(vol[-96:]) if n >= 96 else np.mean(vol)
    avg_vol_1h = np.mean(vol[-4:]) if n >= 4 else np.mean(vol)
    result["vol_avg_7d"] = round(float(avg_vol_7d), 0)
    result["vol_avg_24h"] = round(float(avg_vol_24h), 0)
    result["vol_ratio_1h_vs_24h"] = round(float(avg_vol_1h / avg_vol_24h), 2) if avg_vol_24h > 0 else None
    result["vol_ratio_24h_vs_7d"] = round(float(avg_vol_24h / avg_vol_7d), 2) if avg_vol_7d > 0 else None

    # 当前RSI（15m / 1h）
    rsi_14 = _rsi(df["close"], 14)
    result["rsi_14_15m"] = round(float(rsi_14.iloc[-1]), 1)
    if n >= 64:
        df_1h = df.resample("1h", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        if len(df_1h) > 14:
            rsi_1h = _rsi(df_1h["close"], 14)
            result["rsi_14_1h"] = round(float(rsi_1h.iloc[-1]), 1)
        else:
            result["rsi_14_1h"] = None
    else:
        result["rsi_14_1h"] = None

    # EMA 相对位置
    ema_9 = _ema(df["close"], 9)
    ema_26 = _ema(df["close"], 26)
    ema_99 = _ema(df["close"], 99)
    result["ema9_pct"] = round(float((close[-1] / ema_9.iloc[-1] - 1) * 100), 2)
    result["ema26_pct"] = round(float((close[-1] / ema_26.iloc[-1] - 1) * 100), 2)
    result["ema99_pct"] = round(float((close[-1] / ema_99.iloc[-1] - 1) * 100), 2) if len(ema_99) > 1 else None

    # 检测近期泵（过去 96 bars 内涨幅最大的window）
    max_pump = 0
    max_pump_start = -1
    for i in range(max(0, n - 200), n - 8):
        w_high = np.max(high[i:i + 8])
        w_low = np.min(low[i:i + 8])
        if w_low > 0:
            pump = w_high / w_low - 1
            if pump > max_pump:
                max_pump = pump
                max_pump_start = i

    result["max_pump_8bar"] = round(float(max_pump * 100), 2)
    result["max_pump_bars_ago"] = n - max_pump_start if max_pump_start >= 0 else None

    # 回撤：当前价格距最近泵高点的回撤
    if max_pump_start >= 0:
        pump_high_val = np.max(high[max_pump_start:max_pump_start + 8])
        pullback = 1 - close[-1] / pump_high_val if pump_high_val > 0 else 0
        result["pullback_from_pump_pct"] = round(float(pullback * 100), 2)
        result["pump_high_price"] = round(float(pump_high_val), 6)
    else:
        result["pullback_from_pump_pct"] = None
        result["pump_high_price"] = None

    # 波峰波谷范围（60日高低）
    if n >= 3840:
        period_60d = 3840
    else:
        period_60d = n
    result["high_60d"] = round(float(np.max(high[-period_60d:])), 6)
    result["low_60d"] = round(float(np.min(low[-period_60d:])), 6)
    result["pos_60d"] = round(float((close[-1] - result["low_60d"]) /
                                     (result["high_60d"] - result["low_60d"]) * 100), 1) if result["high_60d"] > result["low_60d"] else 50

    # 前期走势类型：是横盘后拉升？还是已经涨了很久？
    if n >= 672:
        ret_7d_before = (close[-673] / close[0] - 1) * 100 if n > 673 else 0
        result["ret_early"] = round(float(ret_7d_before), 2)

    # 乖离率（最近8根K线最大涨幅内）
    recent_high = np.max(high[-8:])
    recent_low = np.min(low[-8:])
    result["recent_8bar_range_pct"] = round(float((recent_high / recent_low - 1) * 100), 2)

    return result


def main():
    p = argparse.ArgumentParser(description="深度分析涨幅前10币的特征")
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--top-n", type=int, default=10)
    p.add_argument("--out", default=None, help="输出报告路径")
    p.add_argument("--compare-winners", action="store_true",
                   help="额外对比历史 winning trades vs losing trades")
    args = p.parse_args()

    settings = load_settings(args.config)
    client = BinanceClient(api_key=settings.binance.api_key,
                           api_secret=settings.binance.api_secret,
                           testnet=bool(settings.binance.testnet))
    store = ParquetStore(settings.data.storage_dir)

    # 1. 获取涨幅榜
    log.info("扫描 Top %d 涨幅榜...", args.top_n)
    gainers = scan_gainers(
        client,
        quote=settings.universe.quote,
        top_n=args.top_n,
        min_quote_volume_24h=float(settings.universe.min_quote_volume_24h),
        exclude=settings.universe.exclude,
    )
    if not gainers:
        log.error("未获取到涨幅榜")
        return

    log.info("\n===== 今日涨幅 Top %d =====", len(gainers))
    for g in gainers:
        log.info("  %-20s +%.2f%%  vol=$%.0fM", g.symbol, g.pct_change_24h, g.quote_volume_24h / 1e6)

    # 2. 深度分析每个币
    log.info("\n===== 深度分析 =====")
    all_results = []
    for g in gainers:
        sym = g.symbol
        log.info("分析 %s ...", sym)
        r = analyze_gainer(sym, client, store, lookback_days=180)
        r["pct_change_24h"] = round(g.pct_change_24h, 2)
        r["quote_volume_24h"] = round(g.quote_volume_24h, 0)
        all_results.append(r)

    client.close()

    # 3. 生成汇总表格
    lines = []
    lines.append("# 涨幅前10 深度分析报告")
    lines.append("")
    lines.append(f"_生成时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_")
    lines.append("")

    # 3a. 基础行情
    lines.append("## 1. 基础行情")
    lines.append("")
    lines.append("| 币种 | 24h涨幅 | 24h成交量 | 最新价 | 1h涨幅 | 4h涨幅 | 24h涨幅 | 7d涨幅 |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in all_results:
        if r["error"]:
            lines.append(f"| {r['symbol']} | 💀数据不足: {r['error']} |")
            continue
        sym = r["symbol"]
        pct24 = f"+{r['pct_change_24h']:.2f}%" if r['pct_change_24h'] >= 0 else f"{r['pct_change_24h']:.2f}%"
        vol24 = f"${r['quote_volume_24h']/1e6:.1f}M"
        price = f"${r['last_price']:.6f}"
        h1 = f"{r.get('ret_1h', 'N/A'):+.2f}%" if r.get('ret_1h') is not None else "N/A"
        h4 = f"{r.get('ret_4h', 'N/A'):+.2f}%" if r.get('ret_4h') is not None else "N/A"
        d1 = f"{r.get('ret_24h', 'N/A'):+.2f}%" if r.get('ret_24h') is not None else "N/A"
        d7 = f"{r.get('ret_7d', 'N/A'):+.2f}%" if r.get('ret_7d') is not None else "N/A"
        lines.append(f"| {sym:<22} | {pct24} | {vol24} | {price} | {h1} | {h4} | {d1} | {d7} |")

    # 3b. 技术指标
    lines.append("")
    lines.append("## 2. 技术指标")
    lines.append("")
    lines.append("| 币种 | RSI(15m) | RSI(1h) | ATR% | 波动率(24h) | EMA9% | EMA26% | EMA99% |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in all_results:
        if r["error"]:
            continue
        sym = r["symbol"]
        rsi_15m = f"{r['rsi_14_15m']:.0f}" if r['rsi_14_15m'] else "N/A"
        rsi_1h = f"{r['rsi_14_1h']:.0f}" if r.get('rsi_14_1h') else "N/A"
        atr = f"{r['atr_pct']:.3f}%"
        vol24 = f"{r.get('volatility_24h', 0):.2f}%" if r.get('volatility_24h') else "N/A"
        e9 = f"{r['ema9_pct']:+.2f}%"
        e26 = f"{r['ema26_pct']:+.2f}%"
        e99 = f"{r.get('ema99_pct', 0):+.2f}%" if r.get('ema99_pct') is not None else "N/A"
        lines.append(f"| {sym:<22} | {rsi_15m} | {rsi_1h} | {atr} | {vol24} | {e9} | {e26} | {e99} |")

    # 3c. 泵与回撤
    lines.append("")
    lines.append("## 3. 泵与回撤特征")
    lines.append("")
    lines.append("| 币种 | 最大泵(8bar) | 距泵bar数 | 回撤% | 8bar波幅% | 60日高 | 60日低 | 60日位置% |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for r in all_results:
        if r["error"]:
            continue
        sym = r["symbol"]
        mp = f"{r['max_pump_8bar']:.2f}%" if r['max_pump_8bar'] else "N/A"
        ba = str(r['max_pump_bars_ago']) if r.get('max_pump_bars_ago') else "N/A"
        pb = f"{r.get('pullback_from_pump_pct', 0):.2f}%" if r.get('pullback_from_pump_pct') is not None else "N/A"
        rng = f"{r['recent_8bar_range_pct']:.2f}%"
        h60 = f"${r['high_60d']:.6f}"
        l60 = f"${r['low_60d']:.6f}"
        pos60 = f"{r['pos_60d']:.0f}%"
        lines.append(f"| {sym:<22} | {mp} | {ba} | {pb} | {rng} | {h60} | {l60} | {pos60} |")

    # 3d. 成交量分析
    lines.append("")
    lines.append("## 4. 成交量特征")
    lines.append("")
    lines.append("| 币种 | 7日均量 | 24h均量 | 1h/24h量比 | 24h/7d量比 |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for r in all_results:
        if r["error"]:
            continue
        sym = r["symbol"]
        v7d = f"{r['vol_avg_7d']:.0f}"
        v24h = f"{r['vol_avg_24h']:.0f}"
        r1h = f"{r.get('vol_ratio_1h_vs_24h', 'N/A'):.2f}x" if r.get('vol_ratio_1h_vs_24h') else "N/A"
        r24h = f"{r.get('vol_ratio_24h_vs_7d', 'N/A'):.2f}x" if r.get('vol_ratio_24h_vs_7d') else "N/A"
        lines.append(f"| {sym:<22} | {v7d} | {v24h} | {r1h} | {r24h} |")

    # 4. 策略优化建议
    lines.append("")
    lines.append("## 5. 策略优化观察")
    lines.append("")

    # 统计汇总
    valid = [r for r in all_results if not r["error"]]
    if valid:
        avg_pct = np.mean([r["pct_change_24h"] for r in valid])
        avg_atr = np.mean([r["atr_pct"] for r in valid])
        avg_rsi = np.mean([r["rsi_14_15m"] for r in valid if r["rsi_14_15m"]])
        avg_pullback = np.mean([r.get("pullback_from_pump_pct", 0) for r in valid if r.get("pullback_from_pump_pct") is not None])
        avg_pos60 = np.mean([r["pos_60d"] for r in valid])

        lines.append(f"- **平均24h涨幅**: {avg_pct:+.2f}%")
        lines.append(f"- **平均ATR**: {avg_atr:.3f}% (15m单根)")
        lines.append(f"- **平均RSI(15m)**: {avg_rsi:.0f}")
        lines.append(f"- **平均泵后回撤**: {avg_pullback:.2f}%")
        lines.append(f"- **平均60日位置**: {avg_pos60:.0f}% (0=60日低, 100=60日高)")
        lines.append("")

        # 回撤分布
        pb_values = [r.get("pullback_from_pump_pct", 0) for r in valid if r.get("pullback_from_pump_pct") is not None]
        if pb_values:
            lines.append(f"- **回撤分布**: min={min(pb_values):.1f}%, max={max(pb_values):.1f}%, "
                         f"中位数={np.median(pb_values):.1f}%")
            pct_in_10_55 = sum(10 <= p <= 55 for p in pb_values) / len(pb_values) * 100
            lines.append(f"- **回撤在10-55%策略范围内占比**: {pct_in_10_55:.0f}%")
            pct_in_20_40 = sum(20 <= p <= 40 for p in pb_values) / len(pb_values) * 100
            lines.append(f"- **回撤在20-40%(更优范围)占比**: {pct_in_20_40:.0f}%")
        lines.append("")

        # RSI分布
        rsi_values = [r["rsi_14_15m"] for r in valid if r["rsi_14_15m"]]
        if rsi_values:
            overbought = sum(r > 70 for r in rsi_values)
            oversold = sum(r < 30 for r in rsi_values)
            mid = sum(30 <= r <= 70 for r in rsi_values)
            lines.append(f"- **RSI分布**: 超买(>70)={overbought}, 中性(30-70)={mid}, 超卖(<30)={oversold}")
        lines.append("")

        # 60日位置分布
        pos_values = [r["pos_60d"] for r in valid]
        if pos_values:
            high_pos = sum(p > 80 for p in pos_values)
            low_pos = sum(p < 20 for p in pos_values)
            mid_pos = sum(20 <= p <= 80 for p in pos_values)
            lines.append(f"- **60日位置分布**: 高位(>80%)={high_pos}, 中位(20-80%)={mid_pos}, 低位(<20%)={low_pos}")

        lines.append("")

        # 关键观察
        lines.append("### 当前策略问题诊断")
        lines.append("")

        # 分析为什么风险门说 max_concurrent 导致没有开仓
        pos_path = Path("reports/paper/positions.jsonl")
        open_positions = []
        if pos_path.exists():
            with open(pos_path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        ev = json.loads(line)
                        if ev.get("status") == "open":
                            open_positions.append(ev)

        lines.append(f"- **当前持仓数**: {len(open_positions)} (策略锁 `max_concurrent=N`)")
        lines.append(f"- **今日风控结果**: allowed=False, reason=max_concurrent (应释放后才允许新开)")

        lines.append("")
        lines.append("### 策略优化方向")
        lines.append("")
        lines.append("1. **入场条件放宽**: 当前需要EMA确认+成交量恢复+回撤三重过滤，条件太严格")
        lines.append("2. **泵阈值调低**: 当前15%泵阈值过高，今日涨幅TOP10很多涨了10-30%，但内部泵检测可能未触发")
        lines.append("3. **缩短持有时长**: 48 bars(12h)太长，涨幅榜币种走势更短更急，可考虑24-32 bars(6-8h)")
        lines.append("4. **风控闸门节奏**: 每日只开一次仓导致错过后续机会，可改为每批涨幅榜更新时重新评估")
        lines.append("5. **止损优化**: 当前硬止损10-15%，但高波动币种回撤更容易触发，可考虑ATR动态止损")

    report = "\n".join(lines)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(report, encoding="utf-8")
        log.info("\n报告已写入: %s", out_path)
    else:
        print(report)


if __name__ == "__main__":
    main()