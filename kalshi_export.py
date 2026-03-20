"""
Kalshi Export
Pulls the last TREND_DAYS of Dashboard_Summary records from Airtable
and outputs a structured market snapshot — current implied mean, volume,
days to event, and historical trend data for each market.

Run directly:   python3 kalshi_export.py
Import:         from kalshi_export import get_market_data
CoWork:         call get_formatted_report() for display output
"""

import os
import json
from datetime import datetime, timezone, timedelta
from pyairtable import Api

# ── Config ────────────────────────────────────────────────────────────────────

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

# How many days of history to include in trend data
TREND_DAYS = 14

# FF_Rate: only show the nearest upcoming meeting
FF_RATE_NEAREST_ONLY = True

# Markets to include and their display labels
MARKET_LABELS = {
    "CPI":          "CPI (YoY)",
    "Core_CPI_MoM": "Core CPI (MoM)",
    "GDP":          "GDP Growth",
    "Recession":    "Recession Probability",
    "FF_Rate":      "Fed Funds Rate",
    "Payrolls":     "Payrolls",
}

# Markets where implied_mean is a probability (0-1), not a level
PROBABILITY_MARKETS = {"Recession", "FF_Rate"}

# ── Data Fetch ────────────────────────────────────────────────────────────────

def get_airtable_records():
    """Fetch all Dashboard_Summary records from the last TREND_DAYS days."""
    api = Api(AIRTABLE_API_KEY)
    table = api.base(AIRTABLE_BASE_ID).table("Dashboard_Summary")

    cutoff = datetime.now(timezone.utc) - timedelta(days=TREND_DAYS)
    cutoff_str = cutoff.isoformat()

    # Airtable formula to filter by timestamp field
    formula = f"IS_AFTER({{timestamp}}, '{cutoff_str}')"
    records = table.all(formula=formula, sort=["timestamp"])
    return [r["fields"] for r in records]


# ── Data Processing ───────────────────────────────────────────────────────────

def process_records(records):
    """
    Group records by market_type, pick the latest snapshot per market,
    and build the trend series.
    For FF_Rate, filter to the nearest upcoming meeting only.
    """
    from collections import defaultdict

    # Group by market_type → contract_series → list of records
    by_market = defaultdict(lambda: defaultdict(list))
    for r in records:
        market_type = r.get("market_type")
        contract_series = r.get("contract_series", "")
        if market_type:
            by_market[market_type][contract_series].append(r)

    markets_out = {}

    for market_type, series_dict in by_market.items():
        # For FF_Rate, pick the contract_series with the smallest days_to_event
        # from the most recent records (nearest upcoming meeting)
        if market_type == "FF_Rate" and FF_RATE_NEAREST_ONLY:
            # Find the nearest series based on latest record's days_to_event
            def nearest_days(series_records):
                latest = max(series_records, key=lambda r: r.get("timestamp", ""))
                return latest.get("days_to_event", 9999)

            nearest_series = min(series_dict.keys(), key=lambda s: nearest_days(series_dict[s]))
            series_dict = {nearest_series: series_dict[nearest_series]}

        # For all other markets, there should only be one series
        # Merge all series records into one list (handles FF_Rate filtered case)
        all_records = []
        for recs in series_dict.values():
            all_records.extend(recs)

        all_records.sort(key=lambda r: r.get("timestamp", ""))

        if not all_records:
            continue

        latest = all_records[-1]

        # Build trend: one entry per record (timestamp + implied_mean)
        trend = [
            {
                "timestamp": r.get("timestamp"),
                "implied_mean": r.get("implied_mean"),
            }
            for r in all_records
            if r.get("implied_mean") is not None
        ]

        markets_out[market_type] = {
            "label":              MARKET_LABELS.get(market_type, market_type),
            "contract_series":    latest.get("contract_series"),
            "latest_run":         latest.get("timestamp"),
            "implied_mean":       latest.get("implied_mean"),
            "market_volume_usd":  latest.get("market_volume_usd"),
            "days_to_event":      latest.get("days_to_event"),
            "is_probability":     market_type in PROBABILITY_MARKETS,
            "trend":              trend,
        }

    return markets_out


# ── Formatting Helpers ────────────────────────────────────────────────────────

def fmt_mean(market_type, value):
    """Format implied_mean for display."""
    if value is None:
        return "n/a"
    if market_type in PROBABILITY_MARKETS:
        return f"{value * 100:.1f}%"
    if market_type == "Payrolls":
        return f"{int(value):,} jobs"
    return f"{value:.2f}%"


def fmt_volume(value):
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:,}"


def fmt_days(days):
    if days is None:
        return "n/a"
    if days == 0:
        return "Today"
    if days == 1:
        return "1 day"
    return f"{days} days"


def fmt_timestamp(ts):
    if not ts:
        return "n/a"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %I:%M %p UTC")
    except Exception:
        return ts


def trend_summary(market_type, trend):
    """One-line trend description: direction and magnitude vs oldest reading."""
    if len(trend) < 2:
        return "Insufficient data for trend"
    oldest = trend[0]["implied_mean"]
    latest = trend[-1]["implied_mean"]
    if oldest is None or latest is None:
        return "n/a"
    delta = latest - oldest
    if market_type in PROBABILITY_MARKETS:
        delta_str = f"{delta * 100:+.1f}pp"
        latest_str = f"{latest * 100:.1f}%"
        oldest_str = f"{oldest * 100:.1f}%"
    elif market_type == "Payrolls":
        delta_str = f"{int(delta):+,} jobs"
        latest_str = f"{int(latest):,}"
        oldest_str = f"{int(oldest):,}"
    else:
        delta_str = f"{delta:+.2f}%"
        latest_str = f"{latest:.2f}%"
        oldest_str = f"{oldest:.2f}%"

    direction = "▲" if delta > 0 else ("▼" if delta < 0 else "→")
    return f"{direction} {delta_str} over {len(trend)} readings  ({oldest_str} → {latest_str})"


# ── Public API ────────────────────────────────────────────────────────────────

def get_market_data():
    """
    Returns a dict ready for JSON serialization or agent consumption.
    {
      "generated_at": "...",
      "trend_days": 14,
      "markets": { market_type: { ... } }
    }
    """
    records = get_airtable_records()
    markets = process_records(records)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trend_days":   TREND_DAYS,
        "markets":      markets,
    }


def get_formatted_report():
    """
    Returns a formatted text report suitable for CoWork display.
    """
    data = get_market_data()
    generated = fmt_timestamp(data["generated_at"])
    lines = []
    lines.append("=" * 60)
    lines.append(f"  KALSHI MACRO MONITOR")
    lines.append(f"  {generated}")
    lines.append(f"  Trend window: last {data['trend_days']} days")
    lines.append("=" * 60)

    market_order = ["FF_Rate", "CPI", "Core_CPI_MoM", "Payrolls", "GDP", "Recession"]
    markets = data["markets"]

    for market_type in market_order:
        if market_type not in markets:
            continue
        m = markets[market_type]
        lines.append("")
        lines.append(f"  {m['label'].upper()}")
        lines.append(f"  {m['contract_series']}")
        lines.append(f"  {'─' * 40}")
        lines.append(f"  IMPLIED MEAN      {fmt_mean(market_type, m['implied_mean'])}")
        lines.append(f"  TRADING VOLUME    {fmt_volume(m['market_volume_usd'])}")
        lines.append(f"  DAYS TO EVENT     {fmt_days(m['days_to_event'])}")
        lines.append(f"  LAST RUN          {fmt_timestamp(m['latest_run'])}")
        lines.append(f"  TREND             {trend_summary(market_type, m['trend'])}")
        lines.append(f"  READINGS          {len(m['trend'])} in last {data['trend_days']} days")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


# ── Snapshot Writer ───────────────────────────────────────────────────────────

SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "snapshot.json")

def write_snapshot():
    """
    Write the current market data to data/snapshot.json.
    Called by kalshi_monitor.py at the end of each run.
    CoWork reads this file directly — no API calls needed.
    Overwrites the existing file each time.
    """
    data = get_market_data()
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Snapshot written to {SNAPSHOT_PATH}")
    return data


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    # Pass --json flag for raw JSON output (agent/integration use)
    if "--json" in sys.argv:
        data = get_market_data()
        print(json.dumps(data, indent=2))
    else:
        print(get_formatted_report())
