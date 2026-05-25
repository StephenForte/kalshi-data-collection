"""
Kalshi Accuracy Tracker — SQLite edition
Compares Kalshi implied means against realized FRED data releases.
Run weekly (or on demand) to log accuracy for completed events.

Run directly:   python3 kalshi_accuracy.py
                python3 kalshi_accuracy.py --dry-run  (print without writing)

Logic:
  - Finds completed Kalshi events in dashboard_summary (via v_dashboard_summary)
  - Averages implied_mean over the 72 hours before each release
  - Fetches realized value from FRED
  - Writes one row per completed event to accuracy_log
  - Skips events already logged (safe to run multiple times)
"""

import os
import sys
import json
import sqlite3
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config ────────────────────────────────────────────────────────────────────

FRED_API_KEY = os.environ["FRED_API_KEY"]
FRED_BASE_URL = "https://api.stlouisfed.org/fred/series/observations"

DB_PATH = os.path.expanduser(
    os.environ.get("KALSHI_DB_PATH", "~/kalshi-monitor/db/kalshi_macro_data_v2.db")
)

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


# ── FRED API (unchanged) ──────────────────────────────────────────────────────

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

    release_date = datetime.strptime(release_date_str, "%Y-%m-%d").date()

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
        if target_idx < 12:
            return None
        _, prior_value = observations[target_idx - 12]
        if prior_value == 0:
            return None
        return round(((current_value - prior_value) / prior_value) * 100, 4)

    elif transform == "mom":
        if target_idx < 1:
            return None
        _, prior_value = observations[target_idx - 1]
        if prior_value == 0:
            return None
        return round(((current_value - prior_value) / prior_value) * 100, 4)

    elif transform == "mom_jobs":
        if target_idx < 1:
            return None
        _, prior_value = observations[target_idx - 1]
        change_thousands = current_value - prior_value
        return round(change_thousands * 1000, 0)

    return None


# ── SQLite ────────────────────────────────────────────────────────────────────

def open_db():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"SQLite database not found at {DB_PATH}. "
            f"Set KALSHI_DB_PATH or create the database first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def load_market_lookup(conn):
    cur = conn.cursor()
    cur.execute("SELECT code, id FROM markets;")
    return dict(cur.fetchall())


def load_series_lookup(conn):
    """Return {contract_series_code: id}."""
    cur = conn.cursor()
    cur.execute("SELECT code, id FROM contract_series;")
    return dict(cur.fetchall())


def get_completed_events(conn):
    """
    Pull every dashboard_summary row for tracked market types via the view,
    then group by (market_type, contract_series). Returns a dict whose
    values are lists of dicts mirroring the old Airtable shape.
    """
    placeholders = ",".join("?" for _ in MARKET_CONFIG)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT
            market_type,
            contract_series,
            timestamp,
            implied_mean,
            days_to_event
        FROM v_dashboard_summary
        WHERE market_type IN ({placeholders})
        ORDER BY timestamp ASC;
    """, tuple(MARKET_CONFIG.keys()))

    grouped = defaultdict(list)
    for row in cur.fetchall():
        grouped[(row["market_type"], row["contract_series"])].append(dict(row))
    return grouped


def get_already_logged(conn):
    """Return set of contract_series codes already in accuracy_log."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT contract_series FROM v_accuracy_log;")
    return {row[0] for row in cur.fetchall() if row[0]}


def insert_accuracy_row(conn, market_id, series_id, run_date, release_date,
                        kalshi_mean, actual_value, error, abs_error,
                        days_before, readings_in_window):
    """
    Upsert one row into accuracy_log.
    UNIQUE(market_id, release_date) — re-running for the same release updates.
    """
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO accuracy_log
            (market_id, contract_series_id, run_date, release_date,
             kalshi_implied_mean, actual_value, error, abs_error,
             days_before, readings_in_window)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id, release_date) DO UPDATE SET
            contract_series_id  = excluded.contract_series_id,
            run_date            = excluded.run_date,
            kalshi_implied_mean = excluded.kalshi_implied_mean,
            actual_value        = excluded.actual_value,
            error               = excluded.error,
            abs_error           = excluded.abs_error,
            days_before         = excluded.days_before,
            readings_in_window  = excluded.readings_in_window;
    """, (
        market_id, series_id, run_date, release_date,
        kalshi_mean, actual_value, error, abs_error,
        days_before, readings_in_window,
    ))


# ── Inference helpers (unchanged) ─────────────────────────────────────────────

def infer_release_date(market_type, contract_series, records):
    """
    Infer the release date for a completed event.
    Uses the date when days_to_event first hit 0, or the minimum
    days_to_event record date.
    """
    sorted_records = sorted(records, key=lambda r: r.get("timestamp", ""))

    for r in sorted_records:
        if r.get("days_to_event") == 0:
            ts = r.get("timestamp", "")
            if ts:
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    return dt.strftime("%Y-%m-%d")
                except Exception:
                    continue

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

    values = []
    for r in records:
        ts = r.get("timestamp", "")
        if not ts:
            continue
        try:
            record_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if window_start <= record_dt <= release_dt:
                val = r.get("implied_mean")
                if val is not None:
                    values.append(val)
        except Exception:
            continue

    if not values:
        return None

    return round(sum(values) / len(values), 4)


def count_readings_in_window(records, release_date_str):
    release_dt = datetime.strptime(release_date_str, "%Y-%m-%d").replace(
        hour=14, minute=0, tzinfo=timezone.utc
    )
    window_start = release_dt - timedelta(hours=PRE_RELEASE_WINDOW_HOURS)
    n = 0
    for r in records:
        ts = r.get("timestamp", "")
        if not ts or r.get("implied_mean") is None:
            continue
        try:
            rdt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if window_start <= rdt <= release_dt:
                n += 1
        except Exception:
            continue
    return n


# ── Main Logic ────────────────────────────────────────────────────────────────

def run(dry_run=False):
    print(f"\n{'='*60}")
    print(f"Kalshi Accuracy Tracker")
    print(f"Run at: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"DB: {DB_PATH}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Pre-release window: {PRE_RELEASE_WINDOW_HOURS} hours")
    print(f"{'='*60}\n")

    conn = open_db()
    try:
        market_ids = load_market_lookup(conn)
        series_ids = load_series_lookup(conn)
        already_logged = get_already_logged(conn)
        print(f"Already logged: {len(already_logged)} events\n")

        grouped = get_completed_events(conn)
        print(f"Found {len(grouped)} (market_type, contract_series) pairs in dashboard_summary\n")

        results = {"written": 0, "skipped_logged": 0, "skipped_no_data": 0, "skipped_active": 0}

        for (market_type, contract_series), records in sorted(grouped.items()):
            print(f"[{market_type}] {contract_series}")

            if contract_series in already_logged:
                print(f"  Already logged — skipping\n")
                results["skipped_logged"] += 1
                continue

            sorted_records = sorted(records, key=lambda r: r.get("timestamp", ""))
            latest = sorted_records[-1]
            latest_days = latest.get("days_to_event") if latest.get("days_to_event") is not None else 9999
            if latest_days > 0:
                print(f"  Still active ({latest_days} days to event) — skipping\n")
                results["skipped_active"] += 1
                continue

            release_date = infer_release_date(market_type, contract_series, records)
            if not release_date:
                print(f"  Could not infer release date — skipping\n")
                results["skipped_no_data"] += 1
                continue
            print(f"  Release date: {release_date}")

            kalshi_avg = get_pre_release_average(records, release_date)
            if kalshi_avg is None:
                print(f"  No records in {PRE_RELEASE_WINDOW_HOURS}h pre-release window — skipping\n")
                results["skipped_no_data"] += 1
                continue
            print(f"  Kalshi avg ({PRE_RELEASE_WINDOW_HOURS}h pre-release): {kalshi_avg}")

            config = MARKET_CONFIG[market_type]
            actual_value = get_fred_value(config["fred_series"], release_date, config["transform"])
            if actual_value is None:
                print(f"  FRED returned no data for {release_date} — skipping\n")
                results["skipped_no_data"] += 1
                continue
            print(f"  FRED actual value: {actual_value} {config['units']}")

            error     = round(kalshi_avg - actual_value, 4)
            abs_error = round(abs(error), 4)
            print(f"  Error: {error:+.4f} | Abs error: {abs_error:.4f}")

            readings_in_window = count_readings_in_window(records, release_date)
            run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

            # Resolve FK ids
            market_id = market_ids.get(market_type)
            series_id = series_ids.get(contract_series)
            if market_id is None or series_id is None:
                print(f"  Could not resolve FK ids (market={market_id}, series={series_id}) — skipping\n")
                results["skipped_no_data"] += 1
                continue

            record_preview = {
                "market_type":         market_type,
                "contract_series":     contract_series,
                "release_date":        release_date,
                "kalshi_implied_mean": kalshi_avg,
                "actual_value":        actual_value,
                "error":               error,
                "abs_error":           abs_error,
                "days_before":         PRE_RELEASE_WINDOW_HOURS // 24,
                "readings_in_window":  readings_in_window,
                "run_date":            run_date,
            }

            if dry_run:
                print(f"  [DRY RUN] Would write: {json.dumps(record_preview, indent=4)}")
            else:
                insert_accuracy_row(
                    conn,
                    market_id=market_id,
                    series_id=series_id,
                    run_date=run_date,
                    release_date=release_date,
                    kalshi_mean=kalshi_avg,
                    actual_value=actual_value,
                    error=error,
                    abs_error=abs_error,
                    days_before=PRE_RELEASE_WINDOW_HOURS // 24,
                    readings_in_window=readings_in_window,
                )
                conn.commit()
                print(f"  Written to accuracy_log")

            results["written"] += 1
            print()

        print(f"\n{'='*60}")
        print(f"Done.")
        print(f"  Written:          {results['written']}")
        print(f"  Already logged:   {results['skipped_logged']}")
        print(f"  Still active:     {results['skipped_active']}")
        print(f"  No data:          {results['skipped_no_data']}")
        print(f"{'='*60}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    run(dry_run=dry_run)
