"""
Quick test runner for kalshi_export.py
Run from the kalshi-monitor directory with the venv active:
  python3 test_export.py
"""
import os
# Uncomment if running from Cursor's run button (env vars not sourced from .zshrc)
os.environ["AIRTABLE_API_KEY"] = "patMYeEEYpM7fCkPg.740b53a02a317f7763eb228e8abe4cd127e8c20c5441dd6a59966693eb6a3eac"
os.environ["AIRTABLE_BASE_ID"] = "app2uFgJfDU42ZKOm"
os.environ["KALSHI_API_KEY"]   = "bb1e5387-756f-4434-8c3a-dfde46018d35"
from kalshi_export import get_market_data, get_formatted_report

# ── Test 1: Formatted report ───────────────────────────────────────────────
print("TEST 1: Formatted Report")
print(get_formatted_report())

# ── Test 2: Raw JSON structure ─────────────────────────────────────────────
print("\nTEST 2: Raw JSON")
import json
data = get_market_data()
print(json.dumps(data, indent=2))

# ── Test 3: Spot checks on the data ───────────────────────────────────────
print("\nTEST 3: Spot Checks")
markets = data["markets"]
for name, m in markets.items():
    trend_count = len(m.get("trend", []))
    print(f"  {name:20} mean={m['implied_mean']}  vol={m['market_volume_usd']}  trend_points={trend_count}")