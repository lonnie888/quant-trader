"""Real account data tracker - fetches Binance account data and stores history."""
from __future__ import annotations

import hmac
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)

FAPI_BASE_V2 = "https://fapi.binance.com/fapi/v2"
HISTORY_PATH = Path("reports/analysis/real_equity.json")
MAX_HISTORY = 5000  # max snapshots to keep


def _sign(secret: str, params: dict) -> str:
    q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()


def fetch_account(api_key: str, secret: str, proxy: Optional[str] = None) -> dict:
    """Fetch real account data from Binance Futures API."""
    ts = int(time.time() * 1000)
    params = {"timestamp": str(ts), "recvWindow": "10000"}
    sig = _sign(secret, params)
    proxies = {"http": proxy, "https": proxy} if proxy else None
    r = requests.get(
        f"{FAPI_BASE_V2}/account?{'&'.join(f'{k}={v}' for k,v in sorted(params.items()))}&signature={sig}",
        headers={"X-MBX-APIKEY": api_key},
        proxies=proxies,
        timeout=10,
    )
    if r.status_code != 200:
        raise Exception(f"Binance API error: {r.status_code} {r.text}")
    return r.json()


def save_snapshot(api_key: str, secret: str, proxy: Optional[str] = None) -> dict:
    """Fetch current account state and append to history file."""
    acct = fetch_account(api_key, secret, proxy)
    total = float(acct.get("totalWalletBalance", 0) or 0)
    available = float(acct.get("availableBalance", 0) or 0)
    unrealized = float(acct.get("unrealizedProfit", 0) or 0)

    # Parse open positions
    positions = []
    for p in acct.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) < 0.001:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        mark = float(p.get("markPrice", 0) or 0)
        upnl = float(p.get("unrealizedProfit", 0) or 0)
        lev = float(p.get("leverage", 5) or 5)
        margin = abs(amt) * entry / lev if entry > 0 else 0
        pnl_pct = (mark - entry) / entry * 100 if entry > 0 else 0
        side = "LONG" if float(p["positionAmt"]) > 0 else "SHORT"
        positions.append({
            "symbol": p["symbol"],
            "qty": abs(amt),
            "entry": entry,
            "mark": mark,
            "margin": round(margin, 2),
            "unrealizedPnl": round(upnl, 4),
            "pnlPct": round(pnl_pct, 2),
            "side": side,
            "leverage": lev,
        })

    snapshot = {
        "timestamp": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "totalWalletBalance": round(total, 4),
        "availableBalance": round(available, 4),
        "unrealizedProfit": round(unrealized, 4),
        "positions": positions,
        "positionCount": len(positions),
    }

    # Append to history
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if HISTORY_PATH.exists():
        try:
            history = json.loads(HISTORY_PATH.read_text())
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []
    history.append(snapshot)
    # Trim to max
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    HISTORY_PATH.write_text(json.dumps(history, indent=2, ensure_ascii=False))
    log.info("real account snapshot saved: %.2f USDT (%d positions)", total, len(positions))
    return snapshot


def load_history() -> list[dict]:
    """Load equity history from file."""
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text())
    except Exception:
        return []


def get_realtime_summary(api_key: str, secret: str, proxy: Optional[str] = None) -> dict:
    """Get current real account summary (no history)."""
    acct = fetch_account(api_key, secret, proxy)
    total = float(acct.get("totalWalletBalance", 0) or 0)
    available = float(acct.get("availableBalance", 0) or 0)
    unrealized = float(acct.get("unrealizedProfit", 0) or 0)

    positions = []
    for p in acct.get("positions", []):
        amt = float(p.get("positionAmt", 0) or 0)
        if abs(amt) < 0.001:
            continue
        entry = float(p.get("entryPrice", 0) or 0)
        mark = float(p.get("markPrice", 0) or 0)
        upnl = float(p.get("unrealizedProfit", 0) or 0)
        lev = float(p.get("leverage", 5) or 5)
        margin = abs(amt) * entry / lev if entry > 0 else 0
        pnl_pct = (mark - entry) / entry * 100 if entry > 0 else 0
        positions.append({
            "symbol": p["symbol"],
            "qty": abs(amt),
            "entry": entry,
            "mark": mark,
            "margin": round(margin, 2),
            "unrealizedPnl": round(upnl, 4),
            "pnlPct": round(pnl_pct, 2),
            "leverage": lev,
        })

    return {
        "totalWalletBalance": round(total, 2),
        "availableBalance": round(available, 2),
        "unrealizedProfit": round(unrealized, 4),
        "positions": positions,
        "positionCount": len(positions),
    }