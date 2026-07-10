# Quant Trader Daemon (路线 A)

> 从 cron 三套轮询改为常驻进程 + WebSocket 实时推送。

## 架构

```
+----------------------------------------------+
|  quant_trader.scripts.daemon                 |
|  (asyncio 常驻进程)                            |
|                                              |
|  ┌────────────────────────────────────────┐  |
|  │ FapiWS (单连接，多 stream 复用)          │  |
|  │  - wss://fapi.binance.com/ws            │  |
|  │  - 自动重连 + 指数退避                   │  |
|  └────────────────────────────────────────┘  |
|       │                                      |
|       ├─→ KlineStrategyLoop                  |
|       │     订阅 btcusdt@kline_15m ...       |
|       │     k.x=true 时: 更新缓存 + 跑策略   |
|       │     + open_position (风控闸门)        |
|       │                                      |
|       └─→ MarkPriceStream                    |
|             订阅 <sym>@markPrice@1s          |
|             每秒 tick → SL/TP watch 判定     |
|                                              |
|  + 定期任务:                                  |
|    - watchlist refresh (15min, 拉涨幅榜)     |
|    - sltp_refresh (30s, 跟随 open 持仓)       |
+----------------------------------------------+
```

## 与 cron 对比

| 任务 | 旧 cron | 新 daemon |
|:----|:-------|:---------|
| 拉涨幅榜 | scan_incremental 每 15min | watchlist 任务每 15min |
| 检测开仓信号 | scan_incremental 每 15min (用最新 K 线) | kline 推送 `k.x=true` 即时 |
| 拉持仓实时价 | positions_check 每 15min | markPrice 流每秒 |
| 触发 SL/TP | positions_check 15min 检查一次 | markPrice 流 tick-by-tick |
| 拉 K 线数据 | daily_runner 02:00 拉 7 天全量 | kline 流增量 append parquet |

## 启动方式

### 方式 A：手动 (测试用)

```bash
cd /vol1/1000/quant_trader
source .venv/bin/activate
python -m quant_trader.scripts.daemon
```

### 方式 B：systemd (推荐，生产环境)

```bash
# 1. 复制 service 文件
sudo cp deploy/quant-trader-daemon.service /etc/systemd/system/

# 2. 启用
sudo systemctl daemon-reload
sudo systemctl enable quant-trader-daemon
sudo systemctl start quant-trader-daemon

# 3. 查看状态
sudo systemctl status quant-trader-daemon

# 4. 查看日志
journalctl -u quant-trader-daemon -f
# 或
tail -f /vol1/1000/quant_trader/reports/logs/daemon.log
```

### 方式 C：supervisord

```bash
# 1. 复制配置
sudo cp deploy/quant-trader-daemon.conf /etc/supervisor/conf.d/

# 2. 加载
sudo supervisorctl reread
sudo supervisorctl update
sudo supervisorctl start quant-trader-daemon

# 3. 查看
sudo supervisorctl status quant-trader-daemon
tail -f /vol1/1000/quant_trader/reports/logs/daemon.log
```

## cron 改造

替换前：
```cron
0 2 * * *   cd /vol1/1000/quant_trader && /bin/bash deploy/run_daily.sh ...
*/15 * * * *  ... -m quant_trader.scripts.positions_check ...
*/15 * * * *  ... -m quant_trader.scripts.scan_incremental ...
```

替换后：
```cron
# 全部由 daemon 接管，cron 仅保留:
0 2 * * *  /vol1/1000/quant_trader/.venv/bin/python -m quant_trader.scripts.daily_runner --refresh-data
# （可选：每晚 02:00 跑一次 daily_runner 做健康检查 + 全量数据回填）
```

## 关键文件

| 文件 | 用途 |
|:-----|:-----|
| `quant_trader/data/realtime/ws_client.py` | fapi WebSocket 客户端 |
| `quant_trader/data/realtime/kline_strategy.py` | kline 推送 → 策略 → 开仓 |
| `quant_trader/data/realtime/sltp_watch.py` | markPrice 推送 → SL/TP/timeout 平仓 |
| `quant_trader/data/realtime/mark_stream.py` | mark price 状态管理 |
| `quant_trader/scripts/daemon.py` | 主进程 |
| `deploy/quant-trader-daemon.service` | systemd unit |
| `deploy/quant-trader-daemon.conf` | supervisord 配置 |

## 注意事项

- **进程保活**：daemon 崩溃由 systemd/supervisor 自动重启
- **断线重连**：WebSocket 断开会按指数退避 (1s → 60s) 重连
- **本地开发**：Windows 上 `signal.SIGTERM` 不支持 `add_signal_handler`，用 Ctrl+C 即可
- **依赖**：`websockets` Python 包 (`pip install websockets`)

*最后更新: 2026-07-10*