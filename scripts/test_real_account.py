"""Test real account connection without trading.

Connects to real Binance Futures account using settings.yaml credentials,
checks balance and open positions, then exits. Does NOT place any orders.

Usage:
  python scripts/test_real_account.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))

from quant_trader.config import load_settings
from quant_trader.execution.broker import DemoBroker
import hmac, hashlib, requests, time


def main():
    s = load_settings()
    cfg = s.demo_trading
    api_key = cfg.api_key
    secret = cfg.api_secret
    base_url = (getattr(cfg, "base_url", None) or "https://demo-fapi.binance.com").rstrip("/")

    print("=" * 60)
    print("Real Account Connection Test")
    print("=" * 60)
    print(f"base_url: {base_url}")
    print(f"api_key:  {api_key[:8]}...{api_key[-4:]}")
    is_real = "demo" not in base_url.lower() and "testnet" not in base_url.lower()
    print(f"mode:     {'REAL (PRODUCTION)' if is_real else 'DEMO'}")
    print()

    if is_real:
        print("⚠️  WARNING: This will connect to a REAL Binance Futures account!")
        print("    No orders will be placed — read-only test.")
        print()

    proxy = getattr(s, "proxy", None)
    proxies = {"http": proxy, "https": proxy} if proxy else None

    # 1. Get server time
    print("[1] Server time check...")
    try:
        r = requests.get(f"{base_url}/fapi/v1/time", proxies=proxies, timeout=10)
        server_time = r.json()
        local_time = int(time.time() * 1000)
        diff = abs(server_time["serverTime"] - local_time)
        print(f"    Server time: {server_time['serverTime']}")
        print(f"    Local time:  {local_time}")
        print(f"    Diff:        {diff} ms ({'OK' if diff < 1000 else 'WARN: large offset'})")
    except Exception as e:
        print(f"    ERROR: {e}")
        return

    # 2. Get account info (requires valid signature)
    print()
    print("[2] Account info (signed)...")
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": str(ts), "recvWindow": "10000"}
        q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{base_url}/fapi/v2/account?{q}&signature={sig}"
        r = requests.get(url, headers={"X-MBX-APIKEY": api_key}, proxies=proxies, timeout=10)
        if r.status_code == 200:
            acct = r.json()
            print(f"    Total balance:     {acct.get('totalWalletBalance', '?')} USDT")
            print(f"    Available:         {acct.get('availableBalance', '?')} USDT")
            print(f"    Unrealized PnL:    {acct.get('totalUnrealizedProfit', '?')} USDT")
            print(f"    Margin used:        {acct.get('totalInitialMargin', '?')} USDT")
            print(f"    Can trade:          {acct.get('canTrade', '?')}")
            print(f"    Can withdraw:       {acct.get('canWithdraw', '?')}")
            print(f"    Total positions:    {len([p for p in acct.get('positions', []) if float(p.get('positionAmt', 0)) != 0])}")
            # Show open positions
            open_pos = [p for p in acct.get('positions', []) if float(p.get('positionAmt', 0)) != 0]
            for p in open_pos[:5]:
                amt = float(p['positionAmt'])
                print(f"      {p['symbol']} qty={amt} entry={p['entryPrice']} mark={p['markPrice']} pnl={p['unRealizedProfit']}")
        else:
            print(f"    HTTP {r.status_code}: {r.text[:200]}")
            return
    except Exception as e:
        print(f"    ERROR: {e}")
        return

    # 3. Test API key permissions
    print()
    print("[3] API key permissions check...")
    # Try to read positions (no permissions needed beyond read)
    try:
        ts = int(time.time() * 1000)
        params = {"timestamp": str(ts), "recvWindow": "10000"}
        q = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(secret.encode(), q.encode(), hashlib.sha256).hexdigest()
        url = f"{base_url}/fapi/v2/positionRisk?{q}&signature={sig}"
        r = requests.get(url, headers={"X-MBX-APIKEY": api_key}, proxies=proxies, timeout=10)
        if r.status_code == 200:
            data = r.json()
            print(f"    positionRisk: OK ({len(data)} entries)")
        else:
            print(f"    HTTP {r.status_code}: {r.text[:100]}")
    except Exception as e:
        print(f"    ERROR: {e}")

    # 4. Check IP whitelist (server logs will record this)
    print()
    print("=" * 60)
    print("Test complete. Check:")
    print("  - Connection successful")
    print("  - Balance read correctly")
    print("  - No permission errors above")
    if is_real:
        print()
        print("NEXT STEPS (when ready to trade):")
        print("  1. Edit config/settings.yaml:")
        print("     demo_trading:")
        print('       mode: "demo"  # keep as demo for safety')
        print('       base_url: "https://fapi.binance.com"')
        print("  2. Start daemon with real account in demo mode (paper only)")
        print("  3. Monitor for 1 week with real prices, no real orders")
        print("  4. If results match backtest, then change mode to active")
    print("=" * 60)


if __name__ == "__main__":
    main()
