"""Daily orchestration: scan gainers -> update data -> backtest -> rank -> notify."""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quant_trader.config import load_settings  # noqa: E402
from quant_trader.execution.notifier import Notifier, TelegramConfig  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("run_daily")


def _run_module(module: str) -> int:
    log.info("running %s ...", module)
    return subprocess.call([sys.executable, "-m", module])


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="config/settings.yaml")
    p.add_argument("--strategies", default="config/strategies.yaml")
    p.add_argument("--skip-update", action="store_true")
    p.add_argument("--skip-notify", action="store_true")
    args = p.parse_args()

    settings = load_settings(args.config)
    if not args.skip_update:
        rc = _run_module("quant_trader.scripts.update_data")
        if rc != 0:
            log.error("update_data failed rc=%d", rc)
            return rc

    rc = _run_module("quant_trader.scripts.run_backtest")
    if rc != 0:
        log.error("run_backtest failed rc=%d", rc)
        return rc

    if not args.skip_notify and bool(settings.notify.enabled):
        try:
            tg = TelegramConfig(
                bot_token=settings.notify.telegram.bot_token,
                chat_id=settings.notify.telegram.chat_id,
                enabled=True,
            )
            Notifier(tg).send(f"Quant Trader daily run completed at {datetime.utcnow().isoformat()}Z")
        except Exception as e:
            log.warning("notify failed: %s", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
