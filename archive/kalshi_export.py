"""
Kalshi Export
Pulls the last TREND_DAYS of Dashboard_Summary records from Airtable
and outputs a structured market snapshot — current implied mean, volume,
days to event, and historical trend data for each market.

Also pulls the full Accuracy_Log table and groups it by market type
so the dashboard can render forecast-vs-actual history.

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

# How many days back to include already-past annotations (so recent events
# still show on the trendline chart after they resolve)
ANNOTATION_LOOKBACK_DAYS = 14

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

# Day-of-release MAE benchmarks from Diercks, Katz, Wright (FEDS 2026-010),
# Table 3. Kalshi Mean column. Used as a reference point in the accuracy
# block; null where the paper doesn't publish a comparable number.
FED_PAPER_MAE = {
    "CPI":          0.069,   # Headline CPI YoY
    "Core_CPI_MoM": 0.070,   # Core CPI
    "FF_Rate":      0.010,   # Fed Funds Rate
    "Payrolls":     None,
    "GDP":          None,
    "Recession":    None,
}

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


def get_annotations():
    """
    Fetch all Event_Annotations records, returning those within the window:
    - Up to ANNOTATION_LOOKBACK_DAYS in the past
    - Any future date
    Returns list of dicts keyed by market_type.
    """
    api = Api(AIRTABLE_API_KEY)
    table = api.base(AIRTABLE_BASE_ID).table("Event_Annotations")
    records = table.all(sort=["event_date"])

    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANNOTATION_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from collections import defaultdict
    by_market = defaultdict(list)

    for r in records:
        fields = r.get("fields", {})
        event_date = fields.get("event_date", "")
        market_type = fields.get("market_type", "")
        if not event_date or not market_type:
            continue
        # Include if within lookback window or in the future
        if event_date >= cutoff:
            by_market[market_type].append({
                "event_date":  event_date,
                "label":       fields.get("label", ""),
                "type":        fields.get("type", ""),
                "notes":       fields.get("notes", ""),
                "is_past":     event_date < today,
            })

    return by_market


def get_accuracy_records():
    """Fetch all Accuracy_Log records, sorted by release_date ascending."""
    api = Api(AIRTABLE_API_KEY)
    table = api.base(AIRTABLE_BASE_ID).table("Accuracy_Log")
    records = table.all(sort=["release_date"])
    return [r.get("fields", {}) for r in records]


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
            # Find the nearest series with days_to_event > 0 (exclude resolved/today)
            def nearest_days(series_records):
                latest = max(series_records, key=lambda r: r.get("timestamp", ""))
                days = latest.get("days_to_event", 9999)
                # Treat 0 as resolved — push to back so it won't be selected
                return days if days > 0 else 9999

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

        # Build trend: one entry per record (timestamp + implied_mean + moments)
        trend = [
            {
                "timestamp":    r.get("timestamp"),
                "implied_mean": r.get("implied_mean"),
                "std_dev":      r.get("std_dev"),
                "skewness":     r.get("skewness"),
            }
            for r in all_records
            if r.get("implied_mean") is not None
        ]

        markets_out[market_type] = {
            "label":              MARKET_LABELS.get(market_type, market_type),
            "contract_series":    latest.get("contract_series"),
            "latest_run":        latest.get("timestamp"),
            "implied_mean":       latest.get("implied_mean"),
            "std_dev":            latest.get("std_dev"),
            "skewness":           latest.get("skewness"),
            "market_volume_usd":  latest.get("market_volume_usd"),
            "days_to_event":      latest.get("days_to_event"),
            "is_probability":     market_type in PROBABILITY_MARKETS,
            "trend":              trend,
        }

    return markets_out


def process_accuracy(records):
    """
    Group Accuracy_Log records by market_type and compute summary stats.

    Returns a dict keyed by market_type, e.g.:
    {
      "CPI": {
        "label": "CPI (YoY)",
        "release_count": 2,
        "mae": 0.075,
        "rmse": 0.092,
        "fed_paper_mae": 0.069,
        "releases": [
          {"release_date": "2026-04-09", "kalshi_implied_mean": 0.21,
           "actual_value": 0.20, "error": 0.01, "abs_error": 0.01,
           "days_before": 3, "readings_in_window": 12,
           "run_date": "2026-04-12", "contract_series": "..."},
          ...
        ]
      },
      ...
    }
    Releases are sorted most-recent first to match dashboard render order.
    """
    from collections import defaultdict

    by_market = defaultdict(list)
    for r in records:
        market_type = r.get("market_type")
        if not market_type:
            continue
        by_market[market_type].append(r)

    out = {}
    for market_type, recs in by_market.items():
        # Sort releases ascending for stats, then reverse for display
        recs_sorted = sorted(recs, key=lambda r: r.get("release_date", ""))

        releases = []
        abs_errors = []
        sq_errors = []
        for r in recs_sorted:
            kalshi = r.get("kalshi_implied_mean")
            actual = r.get("actual_value")
            err = r.get("error")
            abs_err = r.get("abs_error")

            releases.append({
                "release_date":        r.get("release_date"),
                "run_date":            r.get("run_date"),
                "contract_series":     r.get("contract_series"),
                "kalshi_implied_mean": kalshi,
                "actual_value":        actual,
                "error":               err,
                "abs_error":           abs_err,
                "days_before":         r.get("days_before"),
                "readings_in_window":  r.get("readings_in_window"),
            })

            if abs_err is not None:
                abs_errors.append(abs_err)
                sq_errors.append(abs_err * abs_err)

        n = len(abs_errors)
        mae = sum(abs_errors) / n if n else None
        rmse = (sum(sq_errors) / n) ** 0.5 if n else None

        out[market_type] = {
            "label":          MARKET_LABELS.get(market_type, market_type),
            "release_count":  len(releases),
            "mae":            mae,
            "rmse":           rmse,
            "fed_paper_mae":  FED_PAPER_MAE.get(market_type),
            "releases":       list(reversed(releases)),  # newest first
        }

    return out


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
      "markets":  { market_type: { ..., "annotations": [...] } },
      "accuracy": { market_type: { ..., "releases": [...] } }
    }
    """
    records = get_airtable_records()
    markets = process_records(records)
    annotations = get_annotations()

    # Attach annotations to each market
    for market_type, market_data in markets.items():
        market_data["annotations"] = annotations.get(market_type, [])

    accuracy_records = get_accuracy_records()
    accuracy = process_accuracy(accuracy_records)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trend_days":   TREND_DAYS,
        "markets":      markets,
        "accuracy":     accuracy,
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
    accuracy = data.get("accuracy", {})

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
        if m.get("std_dev") is not None:
            lines.append(f"  STD DEV           {m['std_dev']:.4f}")
        if m.get("skewness") is not None:
            skew_dir = "right-skewed" if m["skewness"] > 0.1 else ("left-skewed" if m["skewness"] < -0.1 else "symmetric")
            lines.append(f"  SKEWNESS          {m['skewness']:.4f}  ({skew_dir})")

        # Accuracy summary, if we have history
        acc = accuracy.get(market_type)
        if acc and acc.get("release_count"):
            mae = acc.get("mae")
            n = acc["release_count"]
            mae_str = f"±{mae:.3f}" if mae is not None else "n/a"
            fed = acc.get("fed_paper_mae")
            fed_str = f"  (Fed paper: ±{fed:.3f})" if fed is not None else ""
            lines.append(f"  ACCURACY          {mae_str} avg error · {n} release{'s' if n != 1 else ''}{fed_str}")

        annotations = m.get("annotations", [])
        if annotations:
            upcoming = [a for a in annotations if not a.get("is_past")]
            past     = [a for a in annotations if a.get("is_past")]
            if upcoming:
                next_ann = upcoming[0]
                lines.append(f"  NEXT EVENT        {next_ann['event_date']}  {next_ann['label']}")
            if past:
                lines.append(f"  RECENT EVENTS     {', '.join(a['label'] for a in past[-2:])}")

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