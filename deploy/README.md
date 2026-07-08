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

## Manual run

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