# Quant Trader - Production Deployment

A Binance USDT-margined perpetual futures paper-trading system. Scans the
top-10 24h gainers every day, applies a locked `pump_pullback` strategy
with a risk gate, persists paper positions, and tracks live PnL against
the public fapi endpoint.

## Layout

```
deploy/
  setup.sh            one-shot: build venv, install deps, run smoke test
  run_daily.sh        full daily pipeline (refresh + scan + risk + ledger + recap)
  quant_trader.cron   crontab fragment (daily 02:00 UTC + every-15min PnL check)
  README.md           this file
```

## First-time setup on the server

```bash
# 1. Pull the project onto the server (adjust path/user)
scp -r D:\project\quant_trader lonnie@192.168.1.210:~/   # or use git

# 2. SSH in
ssh lonnie@192.168.1.210

# 3. Run setup (creates .venv, installs deps, smoke test)
cd ~/quant_trader
bash deploy/setup.sh
```

Expected output: `[setup] DONE.` plus smoke-test result. Takes 3-5 minutes
on a cold cache; under a minute on a warm one.

## Daily operation

The pipeline is driven by `cron`. Install the schedule:

```bash
crontab -l > crontab.bak        # backup existing crontab
cat deploy/quant_trader.cron >> crontab.bak
crontab crontab.bak
crontab -l                      # verify
```

The schedule (all times UTC):

| Time    | Job                                              |
|---------|--------------------------------------------------|
| 02:00   | `run_daily.sh` (refresh + scan + ledger + recap) |
| every 15 min | `positions_check` (live PnL + SL/TP detection)   |

Logs go to `reports/logs/run-YYYY-MM-DD.log` (daily) and
`reports/logs/cron.log` (every cron invocation, including the 15-min one).

Reports go to `reports/paper/`:
  - `YYYY-MM-DD.md`          daily summary (gainers + signals + risk block + open positions)
  - `positions-YYYY-MM-DD.md` live PnL of every open position
  - `recap-YYYY-MM-DD.md`    closed-trade PnL breakdown

## ⚠️ v0.3.0 重要变更：实时 Daemon 替代 cron

**从 v0.3.0 开始，`deploy/quant_trader.cron` 已弃用**，三套轮询脚本被常驻 daemon 替代：

| 旧 cron 任务 | 新 daemon 任务 |
|:----|:----|
| `*/15 positions_check` | markPrice 流每秒推送 |
| `*/15 scan_incremental` | kline 流 `k.x=true` 即时跑策略 |
| `0 2 daily_runner` | 可选保留（健康检查 + 全量回填） |

**如果使用 daemon，请勿同时安装 cron 的 `*/15` 行**，否则会双重入场损坏 ledger。

部署 daemon 详见 [`../docs/daemon.md`](../docs/daemon.md)：
```bash
sudo cp deploy/quant-trader-daemon.service /etc/systemd/system/
sudo systemctl enable --now quant-trader-daemon
```

## 旧版 cron 安装（仅在不用 daemon 时使用）

If you want to trigger a run by hand (e.g. after editing `config/strategies.yaml`):

```bash
cd ~/quant_trader
source .venv/bin/activate
bash deploy/run_daily.sh
```

To bypass the risk gate (testing only, never in production):

```bash
python -m quant_trader.scripts.daily_runner --refresh-data --ignore-risk
```

## Backfill / replay

Replay a past day against the historical gainers file:

```bash
python -m quant_trader.scripts.daily_runner \
    --as-of 2026-07-02 \
    --gainers-file reports/gainers_history.json
```

## Risk configuration

`config/settings.yaml` `risk:` section:

```yaml
risk:
  max_position_pct: 0.10      # single position notional <= 10% of equity
  max_total_exposure: 0.30    # aggregate open positions <= 30% of equity
  daily_loss_limit: 0.05      # halt new entries after -5% on the day (UTC)
  stop_loss_pct: 0.15         # default SL (overridden by strategy params)
  take_profit_pct: 0.0        # default TP (overridden by strategy params)
  max_concurrent: 3           # hard cap on simultaneous open positions
```

## Troubleshooting

- **`ModuleNotFoundError: quant_trader`** — `PYTHONPATH` is set by `run_daily.sh`; if
  running `python` directly, `cd` to the project root and `source .venv/bin/activate`.
- **Binance returns empty** — likely a network/geo issue. Test with
  `curl https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=15m&limit=2`
- **Open position is stuck and never closes** — `positions_check` only detects
  SL/TP/time-exit on bars it has data for. If a symbol gets delisted mid-hold
  the position stays open until `close_position(id)` is called manually.
- **Risk gate always blocks** — clear `reports/paper/positions.jsonl` of stale
  `open` rows (e.g. via a manual script that calls `close_position` with
  `exit_reason="manual"` and current mark price).