"""
Kalshi Accuracy Tracker
Compares Kalshi implied means against realized FRED data releases.
Run weekly (or on demand) to log accuracy for completed events.

Run directly:   python3 kalshi_accuracy.py
               python3 kalshi_accuracy.py --dry-run  (print without writing)

Logic:
  - Finds completed Kalshi events in Dashboard_Summary
  - Averages implied_mean over the 72 hours before each release
  - Fetches realized value from FRED
  - Writes one row per completed event to Accuracy_Log in Airtable
  - Skips events already logged (safe to run multiple times)
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone, timedelta
from pyairtable import Api

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config ────────────────────────────────────────────────────────────────────

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]
FRED_API_KEY     = os.environ["FRED_API_KEY"]

FRED_BASE_URL    = "https://api.stlouisfed.org/fred/series/observations"

# Hours before release to average implied_mean over
PRE_RELEASE_WINDOW_HOURS = 72

# Markets to track and their FRED config
# transform: "yoy" = year-over-year %, "mom" = month-over-month %,
#            "mom_jobs" = monthly change * 1000 (thousands → actual jobs),
#            "level" = use value directly
MARKET_CONFIG = {
    "CPI": {
        "fred_series": "CPIAUCSL",
        "transform":   "yoy",
        "units":       "% YoY",
    },
    "Core_CPI_MoM": {
        "fred_series": "CPILFESL",
        "transform":   "mom",
        "units":       "% MoM",
    },
    "Payrolls": {
        "fred_series": "PAYEMS",
        "transform":   "mom_jobs",
        "units":       "jobs",
    },
    "GDP": {
        "fred_series": "A191RL1Q225SBEA",
        "transform":   "level",
        "units":       "% annualized",
    },
}

# ── FRED API ──────────────────────────────────────────────────────────────────

def fetch_fred_series(series_id, observation_start="2020-01-01"):
    """Fetch all observations for a FRED series. Returns list of (date, value)."""
    params = {
        "series_id":          series_id,
        "api_key":            FRED_API_KEY,
        "file_type":          "json",
        "observation_start":  observation_start,
        "sort_order":         "asc",
    }
    resp = requests.get(FRED_BASE_URL, params=params)
    resp.raise_for_status()
    observations = resp.json().get("observations", [])
    result = []
    for obs in observations:
        if obs.get("value") != ".":  # FRED uses "." for missing
            try:
                result.append((obs["date"], float(obs["value"])))
            except (ValueError, KeyError):
                continue
    return result


def get_fred_value(series_id, release_date_str, transform):
    """
    Return the realized value for a given release date after applying transform.
    release_date_str: "YYYY-MM-DD"
    """
    observations = fetch_fred_series(series_id)
    if not observations:
        return None

    # Find the observation whose date matches or is closest on/before release date
    release_date = datetime.strptime(release_date_str, "%Y-%m-%d").date()

    # Find index of the observation for this release period
    target_idx = None
    for i, (date_str, _) in enumerate(observations):
        obs_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        if obs_date <= release_date:
            target_idx = i
        else:
            break

    if target_idx is None:
        return None

    _, current_value = observations[target_idx]

    if transform == "level":
        return round(current_value, 4)

    elif transform == "yoy":
        # Year-over-year: need observation from ~12 months prior
        if target_idx < 12:
            return None
        _, prior_value = observations[target_idx - 12]
        if prior_value == 0:
            return None
        yoy = ((current_value - prior_value) / prior_value) * 100
        return round(yoy, 4)

    elif transform == "mom":
        # Month-over-month percent change
        if target_idx < 1:
            return None
        _, prior_value = observations[target_idx - 1]
        if prior_value == 0:
            return None
        mom = ((current_value - prior_value) / prior_value) * 100
        return round(mom, 4)

    elif transform == "mom_jobs":
        # Monthly change in thousands → actual jobs
        if target_idx < 1:
            return None
        _, prior_value = observations[target_idx - 1]
        change_thousands = current_value - prior_value
        return round(change_thousands * 1000, 0)

    return None


# ── Airtable ──────────────────────────────────────────────────────────────────

def get_airtable_tables():
    api = Api(AIRTABLE_API_KEY)
    base = api.base(AIRTABLE_BASE_ID)
    return (
        base.table("Dashboard_Summary"),
        base.table("Accuracy_Log"),
    )


def get_completed_events(summary_table):
    """
    Return all unique contract_series that appear to be completed —
    i.e. market_type is in MARKET_CONFIG and we have historical records.
    Groups by (market_type, contract_series) with all their records.
    """
    from collections import defaultdict

    # Fetch all Dashboard_Summary records (no date filter — we want history)
    all_records = summary_table.all(sort=["timestamp"])
    fields_list = [r["fields"] for r in all_records]

    # Group by (market_type, contract_series)
    grouped = defaultdict(list)
    for r in fields_list:
        mt = r.get("market_type")
        cs = r.get("contract_series")
        if mt in MARKET_CONFIG and cs:
            grouped[(mt, cs)].append(r)

    return grouped


def get_already_logged(accuracy_table):
    """Return set of contract_series already in Accuracy_Log."""
    records = accuracy_table.all()
    return {r["fields"].get("contract_series") for r in records if r["fields"].get("contract_series")}


def infer_release_date(market_type, contract_series, records):
    """
    Infer the release date for a completed event.
    Uses the date when days_to_event first hit 0, or the minimum days_to_event record date.
    Returns a date string "YYYY-MM-DD" or None.
    """
    # Sort records by timestamp
    sorted_records = sorted(records, key=lambda r: r.get("timestamp", ""))

    # Find the first record where days_to_event == 0
    for r in sorted_records:
        if r.get("days_to_event") == 0:
            ts = r.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    return dt.strftime("%Y-%m-%d")
                except Exception:
                    continue

    # Fallback: use the timestamp of the record with smallest days_to_event
    min_record = min(sorted_records, key=lambda r: r.get("days_to_event", 9999))
    ts = min_record.get("timestamp", "")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass

    return None


def get_pre_release_average(records, release_date_str):
    """
    Average the implied_mean over records in the PRE_RELEASE_WINDOW_HOURS
    before the release date.
    """
    release_dt = datetime.strptime(release_date_str, "%Y-%m-%d").replace(
        hour=14, minute=0, tzinfo=timezone.utc  # assume ~2pm UTC release
    )
    window_start = release_dt - timedelta(hours=PRE_RELEASE_WINDOW_HOURS)

    window_records = []
    for r in records:
        ts = r.get("timestamp", "")
        if not ts:
            continue
        try:
            record_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if window_start <= record_dt <= release_dt:
                val = r.get("implied_mean")
                if val is not None:
                    window_records.append(val)
        except Exception:
            continue

    if not window_records:
        return None

    return round(sum(window_records) / len(window_records), 4)


# ── Main Logic ────────────────────────────────────────────────────────────────

def run(dry_run=False):
    print(f"\n{'='*60}")
    print(f"Kalshi Accuracy Tracker")
    print(f"Run at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Pre-release window: {PRE_RELEASE_WINDOW_HOURS} hours")
    print(f"{'='*60}\n")

    summary_table, accuracy_table = get_airtable_tables()

    already_logged = get_already_logged(accuracy_table)
    print(f"Already logged: {len(already_logged)} events\n")

    grouped = get_completed_events(summary_table)
    print(f"Found {len(grouped)} (market_type, contract_series) pairs in Dashboard_Summary\n")

    results = {"written": 0, "skipped_logged": 0, "skipped_no_data": 0, "skipped_active": 0}

    for (market_type, contract_series), records in sorted(grouped.items()):
        print(f"[{market_type}] {contract_series}")

        # Skip if already logged
        if contract_series in already_logged:
            print(f"  Already logged — skipping\n")
            results["skipped_logged"] += 1
            continue

        # Skip if still active (days_to_event > 0 in latest record)
        sorted_records = sorted(records, key=lambda r: r.get("timestamp", ""))
        latest = sorted_records[-1]
        latest_days = latest.get("days_to_event", 9999)
        if latest_days > 0:
            print(f"  Still active ({latest_days} days to event) — skipping\n")
            results["skipped_active"] += 1
            continue

        # Infer release date
        release_date = infer_release_date(market_type, contract_series, records)
        if not release_date:
            print(f"  Could not infer release date — skipping\n")
            results["skipped_no_data"] += 1
            continue
        print(f"  Release date: {release_date}")

        # Get pre-release average
        kalshi_avg = get_pre_release_average(records, release_date)
        if kalshi_avg is None:
            print(f"  No records in {PRE_RELEASE_WINDOW_HOURS}h pre-release window — skipping\n")
            results["skipped_no_data"] += 1
            continue
        print(f"  Kalshi avg ({PRE_RELEASE_WINDOW_HOURS}h pre-release): {kalshi_avg}")

        # Fetch FRED actual value
        config = MARKET_CONFIG[market_type]
        actual_value = get_fred_value(config["fred_series"], release_date, config["transform"])
        if actual_value is None:
            print(f"  FRED returned no data for {release_date} — skipping\n")
            results["skipped_no_data"] += 1
            continue
        print(f"  FRED actual value: {actual_value} {config['units']}")

        # Compute error
        error     = round(kalshi_avg - actual_value, 4)
        abs_error = round(abs(error), 4)
        print(f"  Error: {error:+.4f} | Abs error: {abs_error:.4f}")

        # Count readings in window
        release_dt = datetime.strptime(release_date, "%Y-%m-%d").replace(
            hour=14, minute=0, tzinfo=timezone.utc
        )
        window_start = release_dt - timedelta(hours=PRE_RELEASE_WINDOW_HOURS)
        readings_in_window = sum(
            1 for r in records
            if r.get("implied_mean") is not None
            and r.get("timestamp")
            and window_start <= datetime.fromisoformat(
                r["timestamp"].replace("Z", "+00:00")
            ) <= release_dt
        )

        # Write to Accuracy_Log
        record = {
            "market_type":        market_type,
            "contract_series":    contract_series,
            "release_date":       release_date,
            "kalshi_implied_mean": kalshi_avg,
            "actual_value":       actual_value,
            "error":              error,
            "abs_error":          abs_error,
            "days_before":        PRE_RELEASE_WINDOW_HOURS // 24,
            "readings_in_window": readings_in_window,
            "run_date":           datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }

        if dry_run:
            print(f"  [DRY RUN] Would write: {json.dumps(record, indent=4)}")
        else:
            accuracy_table.create(record)
            print(f"  Written to Accuracy_Log")

        results["written"] += 1
        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Done.")
    print(f"  Written:          {results['written']}")
    print(f"  Already logged:   {results['skipped_logged']}")
    print(f"  Still active:     {results['skipped_active']}")
    print(f"  No data:          {results['skipped_no_data']}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
