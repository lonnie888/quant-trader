"""scan_incremental.py — 每15分钟增量扫描涨幅榜，有新币时跑策略开单。

流程：
  1. 读取上次扫描的涨幅榜列表 (reports/paper/last_gainers.json)
  2. 拉取当前涨幅榜 Top 10 (公开 API，无需 Key)
  3. 对比找出新出现的币
  4. 对新币：拉 7 天 15m K 线 → 跑策略 → 风控闸门 → 开单
  5. 更新缓存文件

配合 cron */15 运行，和 positions_check 并行。
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scan_incremental")

FAPI_TICKER = "https://fapi.binance.com/fapi/v1/ticker/24hr"
FAPI_KLINE = "https://fapi.binance.com/fapi/v1/klines"
CACHE_FILE = Path("reports/paper/last_gainers.json")
LOOKBACK_DAYS = 7
TOP_N = 10
EXCLUDE = {"BUSDUSDT", "BTCDOMUSDT", "USDCUSDT"}


def fetch_top_gainers(top_n: int = TOP_N) -> list[str]:
    """返回涨幅前N的币种列表 (ccxt格式: XXX/USDT:USDT)。"""
    r = requests.get(FAPI_TICKER, timeout=15)
    r.raise_for_status()
    all_tickers = r.json()
    candidates = []
    for t in all_tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT") or sym in EXCLUDE:
            continue
        pct = t.get("priceChangePercent")
        vol = float(t.get("quoteVolume", 0))
        if pct is None or vol < 20_000_000:
            continue
        base = sym[:-4]
        ccxt_sym = f"{base}/USDT:USDT"
        candidates.append((ccxt_sym, float(pct)))
    candidates.sort(key=lambda x: -x[1])
    return [c[0] for c in candidates[:top_n]]


def fetch_klines(api_sym: str, days: int = LOOKBACK_DAYS) -> list[list]:
    """用公开 API 拉 K 线，返回原始列表。"""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    out = []
    cursor = start_ms
    while cursor < end_ms:
        r = requests.get(FAPI_KLINE, params={
            "symbol": api_sym, "interval": "15m",
            "startTime": cursor, "endTime": end_ms, "limit": 1000,
        }, timeout=15)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend([row[:6] for row in batch])
        if len(batch) < 1000:
            break
        cursor = batch[-1][0] + 15 * 60 * 1000
    return out


def run():
    # 1. 读取缓存
    last_gainers: list[str] = []
    if CACHE_FILE.exists():
        try:
            last_gainers = json.loads(CACHE_FILE.read_text())
        except Exception:
            pass

    # 2. 拉当前涨幅榜
    try:
        current_gainers = fetch_top_gainers()
    except Exception as e:
        log.warning("涨幅榜拉取失败: %s", e)
        return
    log.info("当前涨幅榜: %s", [s.replace("/USDT:USDT", "") for s in current_gainers])

    # 3. 找出新币
    new_symbols = [s for s in current_gainers if s not in last_gainers]
    if not new_symbols:
        log.info("无新币，但继续对所有涨幅榜币跑策略")
    else:
        log.info("新币出现: %s", [s.replace("/USDT:USDT", "") for s in new_symbols])

    # 4. 对新币跑策略
    from quant_trader.config import load_settings
    from quant_trader.data.fetcher.ohlcv_downloader import download_ohlcv
    from quant_trader.data.fetcher.binance_client import BinanceClient
    from quant_trader.data.storage.parquet_store import ParquetStore
    from quant_trader.strategy.generator.auto_strategy import generate_instances
    from quant_trader.execution.paper_ledger import (
        get_all_positions, get_open_positions, evaluate_risk, open_position, _has_open,
    )

    settings = load_settings()
    store = ParquetStore(settings.data.storage_dir)
    strategies_cfg = "config/strategies.yaml"

    # 用公开 API 拉数据并保存（仅对新币）
    for sym in new_symbols:
        api_sym = sym.split("/")[0].split(":")[0] + "USDT"
        try:
            raw = fetch_klines(api_sym)
        except Exception as e:
            log.warning("%s K线拉取失败: %s", sym, e)
            continue
        if not raw:
            continue
        import pandas as pd
        df = pd.DataFrame(raw, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
        df = df.set_index("timestamp").astype(float)
        store.save(sym, "15m", df)

    # 对当前涨幅榜 ALL 币跑策略（跳过已有持仓）
    instances = generate_instances(strategies_cfg)
    log.info("策略变体数: %d", len(instances))

    positions_path = Path("reports/paper/positions.jsonl")
    risk_cfg = settings.risk
    risk_check = {
        "initial_capital": float(settings.backtest.initial_capital),
        "max_position_pct": float(risk_cfg.max_position_pct),
        "max_total_exposure": float(risk_cfg.max_total_exposure),
        "daily_loss_limit": float(risk_cfg.daily_loss_limit),
        "max_concurrent": int(risk_cfg.max_concurrent),
    }
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    opened = 0
    blocked = 0

    # 收集最近 timeout 退出的币（冷却期），不让它们立刻重新开仓
    cooldown_symbols: set[str] = set()
    now = datetime.now(timezone.utc)
    for e in get_all_positions(positions_path):
        if e.get("status") == "closed" and e.get("exit_reason") == "time":
            exit_ts = e.get("exit_ts")
            if exit_ts:
                try:
                    exit_dt = datetime.fromisoformat(exit_ts.replace("Z", "+00:00"))
                    if (now - exit_dt).total_seconds() < 3600:  # 1小时冷却
                        cooldown_symbols.add(e["symbol"])
                except Exception:
                    pass
    if cooldown_symbols:
        log.info("冷却期符号(1h内timeout退出): %s", list(cooldown_symbols))

    # 批量拉取所有币种实时价格（替代 N+1 次单独请求）
    price_cache: dict[str, float] = {}
    try:
        pr = requests.get("https://fapi.binance.com/fapi/v1/ticker/price", timeout=10)
        pr.raise_for_status()
        for p in pr.json():
            price_cache[p["symbol"]] = float(p["price"])
    except Exception as e:
        log.warning("批量价格拉取失败，回退逐币查询: %s", e)

    for sym in current_gainers:
        if sym in cooldown_symbols:
            log.info("跳过: %s 在冷却期内（刚timeout退出）", sym)
            continue
        if _has_open(get_all_positions(positions_path), sym):
            log.info("跳过: %s 已有持仓", sym)
            continue
        df = store.load(sym, "15m")
        if df.empty or len(df) < 100:
            continue
        for name, params, strat in instances:
            try:
                sigs = strat.generate_signals(df)
            except Exception as e:
                log.warning("%s 策略失败: %s", sym, e)
                continue
            if sigs.empty:
                continue
            s = sigs.values
            last_entry = -1
            prev = 0
            for i, v in enumerate(s):
                if v == 1 and prev == 0:
                    last_entry = i
                prev = v
            if s[-1] == 1 and last_entry >= 0:
                bars_since = len(s) - 1 - last_entry
                if bars_since > 2:
                    log.warning("跳过 %s: 信号滞后 %d 根K线(追高防护)", sym, bars_since)
                    continue
                # 有信号：取当前实时市价开单，而非K线收盘价
                now_ts = datetime.now(timezone.utc).isoformat()
                current_api_sym = sym.split("/")[0].split(":")[0] + "USDT"
                cached_price = price_cache.get(current_api_sym)
                if cached_price is not None:
                    entry_price = cached_price
                else:
                    try:
                        ticker_r = requests.get(FAPI_TICKER, params={"symbol": current_api_sym}, timeout=10)
                        ticker_r.raise_for_status()
                        ticker_data = ticker_r.json()
                        entry_price = float(ticker_data.get("lastPrice", df.iloc[-1]["close"]))
                    except Exception:
                        entry_price = float(df.iloc[-1]["close"])
                entry_ts = now_ts

                # 风控
                all_events_now = get_all_positions(positions_path)
                allowed, reason = evaluate_risk(all_events_now, **risk_check)
                if not allowed:
                    log.info("风控阻挡 %s: %s", sym, reason)
                    blocked += 1
                    open_position(
                        symbol=sym, strategy=name, params=params,
                        entry_ts=entry_ts, entry_price=entry_price,
                        leverage=float(settings.backtest.leverage),
                        open_day=today, log_path=positions_path,
                        risk_check=risk_check,
                    )
                    continue

                # 开单
                ev = open_position(
                    symbol=sym, strategy=name, params=params,
                    entry_ts=entry_ts, entry_price=entry_price,
                    leverage=float(settings.backtest.leverage),
                    open_day=today, log_path=positions_path,
                    risk_check=risk_check,
                )
                if ev is not None and ev.status == "open":
                    opened += 1
                    log.info("✅ 开单 %s @ %.6f id=%d", sym, entry_price, ev.id)

    # 5. 更新缓存
    CACHE_FILE.write_text(json.dumps(current_gainers))
    log.info("增量扫描完成，本次开单: %d", opened)

    # 飞书通知
    if opened > 0 or blocked > 0:
        try:
            from quant_trader.execution.notifier import FeishuNotifier, FeishuCardBuilder
            gainer_pairs = [(s.replace("/USDT:USDT", "") if "/USDT:USDT" in s else s, 0.0) for s in current_gainers[:30]]
            feishu = FeishuNotifier()
            card = FeishuCardBuilder.make_daily_summary(
                as_of=today, gainers=gainer_pairs,
                accepted=opened, blocked=blocked,
                open_pos=len(get_open_positions(positions_path)),
            )
            feishu.send_card(card)
        except Exception:
            pass


if __name__ == "__main__":
    run()