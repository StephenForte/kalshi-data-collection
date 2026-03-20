"""
Quick test runner for kalshi_export.py
Run from the kalshi-monitor directory with the venv active:
  python3 test_export.py
"""
import os

required_vars = ("AIRTABLE_API_KEY", "AIRTABLE_BASE_ID")
missing = [name for name in required_vars if not os.environ.get(name)]
if missing:
    raise RuntimeError(
        "Missing required environment variable(s): "
        + ", ".join(missing)
        + ". Export them before running test_export.py."
    )

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