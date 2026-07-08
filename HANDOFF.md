# Quant Trader - Project Handoff

## What this is

A **paper-trading** system for Binance USDT-margined perpetual futures. It:

1. scans the top-10 24h gainers on USDT-M perps
2. applies a single locked strategy (`pump_pullback`)
3. enforces a hard risk gate before opening any position
4. persists every open/close/blocked event to an append-only ledger
5. tracks live PnL against the public fapi endpoint

**No real orders are ever sent.** It exists to validate that the strategy
edge survives contact with live data and live risk events (SL/TP/time exits).

## Architecture (1 minute)

```
[Binance fapi public]                  [Binance REST (ccxt, needs API key)]
        |                                           |
        v                                           v
refresh_fapi.py  ------------------->  update_data.py
        |                                           |
        v                                           v
       data_store/{SYM}_USDT_USDT/15m.parquet  (138 symbols, 7d window)
                        |
                        v
                  daily_runner.py  --[risk gate]-->  paper_ledger.py
                        |                                    |
                        v                                    v
       reports/paper/YYYY-MM-DD.jsonl        reports/paper/positions.jsonl
                        |                                    |
                        v                                    v
                   recap.py  <--------------  positions_check.py
                        |                                    |
                        v                                    v
       reports/paper/recap-YYYY-MM-DD.md   reports/paper/positions-YYYY-MM-DD.md
```

## The locked strategy: `pump_pullback`

Entry pattern: detect a recent pump (>= +20% in 12 bars), wait for a pullback
into the 15-55% retracement zone, confirm a second-leg trigger via EMA + volume,
then long for 48 bars (12h) with a -15% hard stop. No take-profit (lets winners
run). Long-only. All tunables are in `config/strategies.yaml`.

8 other strategies are registered (`ma_cross`, `macd`, `rsi`, `bollinger`,
`kdj`, `turtle`, `breakout`, `mean_reversion`) but are not active. To re-enable
them add blocks under `config/strategies.yaml`.

## The risk gate

`config/settings.yaml` `risk:` section, evaluated in
`quant_trader/execution/paper_ledger.py:evaluate_risk` before any open:

| limit                  | current | meaning                              |
|------------------------|---------|--------------------------------------|
| `max_position_pct`     | 0.10    | single position notional cap         |
| `max_total_exposure`   | 0.30    | aggregate open positions cap         |
| `max_concurrent`       | 3       | hard cap on simultaneous open slots  |
| `daily_loss_limit`     | 0.05    | halt new entries after -5% on the day|

A rejected signal still gets logged as `status=blocked` so the audit trail
is complete.

## File map (only files you need to know)

| Path                                          | Why you would touch it                                  |
|-----------------------------------------------|--------------------------------------------------------|
| `config/settings.yaml`                        | risk limits, universe filter, scoring weights           |
| `config/strategies.yaml`                      | which strategies are active + their parameter space    |
| `quant_trader/strategy/library/pump_pullback.py` | the strategy logic; SL/TP/bar-internal triggers       |
| `quant_trader/execution/paper_ledger.py`      | open/close/blocked events; risk evaluation             |
| `quant_trader/scripts/daily_runner.py`        | the production entry point                             |
| `quant_trader/scripts/positions_check.py`     | live PnL + SL/TP detection                             |
| `quant_trader/scripts/recap.py`               | closed-trade PnL breakdown                             |
| `quant_trader/scripts/refresh_fapi.py`       | public fapi kline refresh (no auth)                    |
| `quant_trader/scripts/tune_pump_pullback.py`  | walk-forward parameter tuning                          |
| `deploy/setup.sh`                            | server one-shot: venv + deps + smoke test              |
| `deploy/run_daily.sh`                        | full daily pipeline (refresh + scan + risk + recap)    |
| `deploy/quant_trader.cron`                   | crontab: 02:00 UTC daily + every 15 min positions check |
| `deploy/README.md`                           | server deployment + troubleshooting                    |
| `data_store/`                                 | local parquet cache; per-symbol `SYM_USDT_USDT/15m.parquet` |
| `reports/gainers_history.json`                | 30 days of historical top-10 (used by replay/tuning)  |
| `reports/paper/positions.jsonl`               | the ledger: every open/closed/blocked event ever       |
| `reports/paper/recap-YYYY-MM-DD.md`           | realized PnL per day                                   |

## Day-to-day commands

```bash
# full daily pipeline (refresh + scan + risk + recap)
bash deploy/run_daily.sh

# just check live PnL on open positions (runs every 15 min via cron)
python -m quant_trader.scripts.positions_check

# replay a past day (uses reports/gainers_history.json)
python -m quant_trader.scripts.daily_runner \
    --as-of 2026-07-02 --gainers-file reports/gainers_history.json

# bypass the risk gate (testing only)
python -m quant_trader.scripts.daily_runner --refresh-data --ignore-risk
```

## Current state at handoff

- **3 open positions** in the ledger (TA 7/4 still in hold, FAKE mock for
  risk-gate testing, one historical). All are sandbox/test data — clear
  them before going live.
- **No profit attribution yet**: only ~5 days of replay data on the locked
  strategy, sample size is too small to draw conclusions.
- **No backtest framework in active use**: `quant_trader/backtest/` is
  implemented but not wired into the daily flow. The live forward test is
  the source of truth right now.

## Known sharp edges (read before changing anything)

1. **Data drift.** `data_store/` was originally populated via ccxt. Symbols
   on Binance can change tickers; refresh via `refresh_fapi.py` (public
   endpoint) is the only reliable path. Re-run on any suspicion of stale
   data.

2. **`simulate_hold` returns a frozen list, not a DataFrame.** The bar-list
   format drops the datetime index; always use `reset_index()` before
   passing to it. There is a `replay_day.py` example.

3. **`update_data.py` requires a valid Binance API key** for the ccxt
   ticker scan. `refresh_fapi.py` does not. If the API key is a placeholder,
   `daily_runner` will fall back to whatever is in the local parquet.

4. **CRLF/BOM in shell scripts.** `deploy/*.sh` must be LF, no BOM. If
   they come from a Windows editor with CRLF endings the server's
   `/bin/bash` will reject them with `env: No such file or directory`.
   `dos2unix` or `sed -i 's/\r$//'`.

5. **`recap.py` exit_reason `data_end`.** When a position is still in hold
   the simulated PnL uses the most recent bar's close. Wait for
   `bars_left=0` or a manual `close_position` to get a frozen PnL.

6. **Stop-loss is bar-internal, not bar-close.** `pump_pullback` checks
   `low[i] <= entry * (1 - stop_loss_pct)` per bar; the `recap.py` simulator
   matches this. If you write a new simulator, copy that check.

## Things to do first when you pick this up

1. **Clear `reports/paper/positions.jsonl`** of the test/mock rows.
2. **Decide on hosting.** Either ARM server (192.168.1.210, see
   `deploy/README.md`) or local-only. SSH access to the server was
   unconfirmed at handoff; `scp` or `git clone` is the intended handoff
   path.
3. **Run `bash deploy/setup.sh` once** to validate the venv + deps build
   on the target machine.
4. **Install the cron** (one-time): `crontab deploy/quant_trader.cron`.
5. **Wait 7-10 days** of accumulated data before drawing any strategy
   conclusions. The locked parameters are a single walk-forward pick on
   138 symbols; they are not validated out-of-sample.
6. **Telegram notifier** (`quant_trader/execution/notifier.py`) is
   implemented but disabled. Wire it up in `config/settings.yaml` `notify:`
   if you want daily push summaries.

## Open questions for the next person

- Does the user want **out-of-sample validation** before trusting the
  locked parameters? `tune_pump_pullback.py` already does walk-forward on
  the 138-symbol pool; a dedicated OOS script would take ~1 hour to write.
- Should the **simulator** be moved out of `recap.py` into a shared
  `quant_trader/sim/` module? `replay_day.py` and `recap.py` and
  `tune_pump_pullback.py` each carry their own copy.
- Real order routing is intentionally not built. If/when that is wanted,
  `quant_trader/execution/paper_trader.py` is the right place to start
  (it has the Broker/Portfolio scaffolding but no Binance-ccxt submit
  wiring yet).

## Environment at handoff

- Python 3.10.6, Windows host
- venv at `D:\project\quant_trader\.venv\`
- 138 symbols in `data_store/`, 7 days of 15m klines each
- No `requirements.txt` lockfile; `deploy/setup.sh` installs from
  `requirements.txt` (ccxt, pandas, numpy, pyarrow, pyyaml, scipy,
  scikit-learn, ta)
- 3491 LOC of Python across 50 files; the production hot path is
  roughly 800 LOC in `scripts/` + `execution/paper_ledger.py`