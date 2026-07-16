"""
Kalshi Export — SQLite edition (v2: with distributions and FF_Rate meetings)

Reads the last TREND_DAYS of dashboard_summary rows from the SQLite database
and outputs a structured market snapshot — current implied mean, volume,
days to event, historical trend data, event annotations, an accuracy block
computed from accuracy_log rows, and a top-3-by-probability distribution
slice per market.

Run directly:   python3 kalshi_export.py
Import:         from kalshi_export import get_market_data
Snapshot file:  call write_snapshot()

BREAKING CHANGE vs v1: FF_Rate is now a list of meetings under a `meetings`
key, sorted nearest-first by days_to_event. The local dashboard must handle
this shape (handled in kalshi_dashboard.html).
"""

import math
import os
import re
import sqlite3
import sys
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config ────────────────────────────────────────────────────────────────────

DB_PATH = os.path.expanduser(
    os.environ.get("KALSHI_DB_PATH", "~/kalshi-monitor/db/kalshi_macro_data_v2.db")
)

TREND_DAYS = 14
ANNOTATION_LOOKBACK_DAYS = 14
TOP_N_DISTRIBUTION = 3

MARKET_LABELS = {
    "CPI":          "CPI (YoY)",
    "Core_CPI_MoM": "Core CPI (MoM)",
    "GDP":          "GDP Growth",
    "Recession":    "Recession Probability",
    "FF_Rate":      "Fed Funds Rate",
    "Payrolls":     "Payrolls",
}

PROBABILITY_MARKETS = {"Recession", "FF_Rate"}
CUMULATIVE_MARKETS  = {"CPI", "Core_CPI_MoM", "GDP", "Payrolls"}
ME_MARKETS          = {"FF_Rate", "Recession"}

FED_PAPER_MAE = {
    "CPI":          0.069,
    "Core_CPI_MoM": 0.070,
    "GDP":          None,
    "Payrolls":     None,
}

SNAPSHOT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "snapshot.json"
)

# ── GitHub push config ────────────────────────────────────────────────────────
# Push snapshot.json to a public repo so GitHub Pages can serve it.
# Auth uses a Personal Access Token in the GITHUB_TOKEN env var. If the
# token is missing, the push is silently skipped (handy for local dev).
GITHUB_REPO     = os.environ.get("KALSHI_GITHUB_REPO",   "StephenForte/macro_dashboard")
GITHUB_BRANCH   = os.environ.get("KALSHI_GITHUB_BRANCH", "main")
GITHUB_PATH     = os.environ.get("KALSHI_GITHUB_PATH",   "data/snapshot.json")
GITHUB_API_BASE = "https://api.github.com"
GITHUB_TIMEOUT  = 10  # seconds


# ── DB plumbing ───────────────────────────────────────────────────────────────

def open_db():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"SQLite database not found at {DB_PATH}. "
            f"Set KALSHI_DB_PATH or create the database first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Data Fetch ────────────────────────────────────────────────────────────────

def fetch_dashboard_rows(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=TREND_DAYS)).isoformat()
    cur = conn.cursor()
    cur.execute("""
        SELECT
            timestamp,
            market_type,
            contract_series,
            implied_mean,
            std_dev,
            skewness,
            market_volume_usd,
            days_to_event,
            strike_structure
        FROM v_dashboard_summary
        WHERE timestamp > ?
        ORDER BY timestamp ASC;
    """, (cutoff,))
    return [dict(row) for row in cur.fetchall()]


def fetch_annotations(conn):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ANNOTATION_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    cur = conn.cursor()
    cur.execute("""
        SELECT market_type, event_date, label, type, notes
        FROM v_event_annotations
        WHERE event_date >= ?
        ORDER BY event_date ASC;
    """, (cutoff,))

    by_market = defaultdict(list)
    for row in cur.fetchall():
        by_market[row["market_type"]].append({
            "event_date": row["event_date"],
            "label":      row["label"] or "",
            "type":       row["type"] or "",
            "notes":      row["notes"] or "",
            "is_past":    row["event_date"] < today,
        })
    return by_market


def fetch_accuracy_rows(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT
            market_type,
            contract_series,
            run_date,
            release_date,
            kalshi_implied_mean,
            actual_value,
            error,
            abs_error,
            days_before,
            readings_in_window
        FROM v_accuracy_log
        ORDER BY release_date DESC;
    """)

    by_market = defaultdict(list)
    for row in cur.fetchall():
        by_market[row["market_type"]].append({
            "release_date":         row["release_date"],
            "run_date":             row["run_date"],
            "contract_series":      row["contract_series"],
            "kalshi_implied_mean":  row["kalshi_implied_mean"],
            "actual_value":         row["actual_value"],
            "error":                row["error"],
            "abs_error":            row["abs_error"],
            "days_before":          row["days_before"],
            "readings_in_window":   row["readings_in_window"],
        })
    return by_market


def fetch_latest_distributions(conn):
    """
    For each (market_type, contract_series), pull the latest snapshot's
    per-contract rows. We compute the latest timestamp per series (rather
    than a global MAX) because different events may be updated on slightly
    different cadences.
    """
    cur = conn.cursor()
    cur.execute("""
        WITH latest_ts AS (
            SELECT
                c.contract_series_id,
                MAX(cd.timestamp) AS ts
            FROM contract_details cd
            JOIN contracts c ON c.id = cd.contract_id
            WHERE c.is_synthetic = 0
            GROUP BY c.contract_series_id
        )
        SELECT
            m.code              AS market_type,
            cs.code             AS contract_series,
            c.code              AS contract_code,
            c.strike            AS strike,
            c.label             AS contract_label,
            cd.yes_price,
            cd.implied_probability,
            cd.volume,
            cd.timestamp
        FROM contract_details cd
        JOIN contracts       c  ON c.id  = cd.contract_id
        JOIN contract_series cs ON cs.id = c.contract_series_id
        JOIN markets         m  ON m.id  = cs.market_id
        JOIN latest_ts       lt ON lt.contract_series_id = cs.id
                               AND lt.ts = cd.timestamp
        WHERE c.is_synthetic = 0
        ORDER BY cs.code, c.strike;
    """)

    by_series = defaultdict(list)
    for row in cur.fetchall():
        by_series[(row["market_type"], row["contract_series"])].append(dict(row))
    return by_series


# ── Distribution shaping ──────────────────────────────────────────────────────

QUARTER_LABEL_RE = re.compile(r"^Q([1-4])\s+(\d{4})$")


def _quarter_is_past(label, now=None):
    """True if 'Qn YYYY' refers to a quarter that has already ended."""
    m = QUARTER_LABEL_RE.match(label or "")
    if not m:
        return False
    q = int(m.group(1))
    y = int(m.group(2))
    quarter_end_month = {1: 3, 2: 6, 3: 9, 4: 12}[q]
    quarter_end_day   = {1: 31, 2: 30, 3: 30, 4: 31}[q]
    quarter_end = datetime(y, quarter_end_month, quarter_end_day, tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return quarter_end < now


def _format_strike_label(market_type, strike, next_strike):
    """Build a human label for a cumulative-market strike bin."""
    if market_type == "Payrolls":
        # Payrolls strikes are in thousands of jobs
        lo = int(round(strike))
        if next_strike is None:
            return f"≥ {lo:,}k jobs"
        hi = int(round(next_strike))
        return f"{lo:,}k–{hi:,}k jobs"

    # Percentage-based markets
    if next_strike is None:
        return f"≥ {strike:g}%"
    return f"{strike:g}%–{next_strike:g}%"


def _build_cumulative_distribution(rows):
    """For cumulative markets — sort by strike, label each bin."""
    if not rows:
        return []
    sorted_rows = sorted(
        rows,
        key=lambda r: r["strike"] if r["strike"] is not None else float("inf")
    )
    market_type = sorted_rows[0]["market_type"]

    out = []
    for i, r in enumerate(sorted_rows):
        next_strike = sorted_rows[i + 1]["strike"] if i + 1 < len(sorted_rows) else None
        out.append({
            "label":       _format_strike_label(market_type, r["strike"], next_strike),
            "strike":      r["strike"],
            "probability": r["implied_probability"],
            "yes_price":   r["yes_price"],
            "volume":      r["volume"],
        })
    return out


def _build_me_distribution(rows, exclude_past_quarters=False):
    """For mutually-exclusive markets — each row is a discrete outcome."""
    out = []
    for r in rows:
        label = r["contract_label"] or r["contract_code"] or ""
        if exclude_past_quarters and _quarter_is_past(label):
            continue
        out.append({
            "label":       label,
            "strike":      None,
            "probability": r["implied_probability"],
            "yes_price":   r["yes_price"],
            "volume":      r["volume"],
        })
    return out


def _top_n_by_probability(distribution, n=TOP_N_DISTRIBUTION):
    return sorted(
        distribution,
        key=lambda d: d["probability"] if d["probability"] is not None else -1.0,
        reverse=True,
    )[:n]


def build_distribution_slice(market_type, contract_rows):
    """Given contract rows for a single event, return a top-N distribution slice."""
    if not contract_rows:
        return []

    if market_type in CUMULATIVE_MARKETS:
        full = _build_cumulative_distribution(contract_rows)
    elif market_type == "Recession":
        full = _build_me_distribution(contract_rows, exclude_past_quarters=True)
    elif market_type == "FF_Rate":
        full = _build_me_distribution(contract_rows, exclude_past_quarters=False)
    else:
        full = []

    return _top_n_by_probability(full, n=TOP_N_DISTRIBUTION)


# ── Data Processing ───────────────────────────────────────────────────────────

def _series_payload_from_records(records, series_code, distribution):
    """Build the per-series chunk: latest aggregates + trend + distribution."""
    records = sorted(records, key=lambda r: r.get("timestamp", ""))
    latest = records[-1]

    trend = [
        {
            "timestamp":    r.get("timestamp"),
            "implied_mean": r.get("implied_mean"),
            "std_dev":      r.get("std_dev"),
            "skewness":     r.get("skewness"),
        }
        for r in records
        if r.get("implied_mean") is not None
    ]

    return {
        "contract_series":    series_code,
        "latest_run":         latest.get("timestamp"),
        "implied_mean":       latest.get("implied_mean"),
        "std_dev":            latest.get("std_dev"),
        "skewness":           latest.get("skewness"),
        "market_volume_usd":  latest.get("market_volume_usd"),
        "days_to_event":      latest.get("days_to_event"),
        "trend":              trend,
        "distribution":       distribution,
    }


def process_records(records, distributions_by_series):
    """
    Group records by market_type → contract_series. FF_Rate is special:
    returns a `meetings` list (nearest first).
    """
    by_market = defaultdict(lambda: defaultdict(list))
    for r in records:
        market_type = r.get("market_type")
        contract_series = r.get("contract_series", "")
        if market_type:
            by_market[market_type][contract_series].append(r)

    markets_out = {}

    for market_type, series_dict in by_market.items():

        if market_type == "FF_Rate":
            meetings = []
            for series_code, recs in series_dict.items():
                contract_rows = distributions_by_series.get((market_type, series_code), [])
                distribution = build_distribution_slice(market_type, contract_rows)
                meetings.append(_series_payload_from_records(
                    recs, series_code, distribution
                ))
            # Sort nearest first; treat unknown/None as "far"
            meetings.sort(key=lambda m: m["days_to_event"] if m["days_to_event"] is not None else 9999)

            markets_out["FF_Rate"] = {
                "label":          MARKET_LABELS["FF_Rate"],
                "is_probability": True,
                "meetings":       meetings,
            }
            continue

        # Non-FF_Rate: there's typically one active series per market
        all_records = []
        for recs in series_dict.values():
            all_records.extend(recs)
        all_records.sort(key=lambda r: r.get("timestamp", ""))

        if not all_records:
            continue

        latest = all_records[-1]
        latest_series = latest.get("contract_series")

        contract_rows = distributions_by_series.get((market_type, latest_series), [])
        distribution = build_distribution_slice(market_type, contract_rows)

        payload = _series_payload_from_records(all_records, latest_series, distribution)
        payload["label"]          = MARKET_LABELS.get(market_type, market_type)
        payload["is_probability"] = market_type in PROBABILITY_MARKETS

        markets_out[market_type] = payload

    return markets_out


def build_accuracy_block(accuracy_by_market):
    out = {}
    for market_type, releases in accuracy_by_market.items():
        if market_type not in FED_PAPER_MAE:
            continue

        abs_errors = [r["abs_error"] for r in releases if r.get("abs_error") is not None]
        if not abs_errors:
            continue

        mae = sum(abs_errors) / len(abs_errors)
        rmse = math.sqrt(sum(e * e for e in abs_errors) / len(abs_errors))

        out[market_type] = {
            "label":         MARKET_LABELS.get(market_type, market_type),
            "release_count": len(releases),
            "mae":           mae,
            "rmse":          rmse,
            "fed_paper_mae": FED_PAPER_MAE.get(market_type),
            "releases":      releases,
        }
    return out


# ── Formatting Helpers ────────────────────────────────────────────────────────

def fmt_mean(market_type, value):
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
    conn = open_db()
    try:
        dashboard_rows  = fetch_dashboard_rows(conn)
        annotations     = fetch_annotations(conn)
        accuracy_rows   = fetch_accuracy_rows(conn)
        distributions   = fetch_latest_distributions(conn)
    finally:
        conn.close()

    markets = process_records(dashboard_rows, distributions)

    for market_type, market_data in markets.items():
        market_data["annotations"] = annotations.get(market_type, [])

    accuracy = build_accuracy_block(accuracy_rows)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trend_days":   TREND_DAYS,
        "markets":      markets,
        "accuracy":     accuracy,
    }


def _append_market_section(lines, market_type, label, payload):
    """Render one event-level chunk into the text report."""
    lines.append("")
    lines.append(f"  {label.upper()}")
    lines.append(f"  {payload['contract_series']}")
    lines.append(f"  {'─' * 40}")
    lines.append(f"  IMPLIED MEAN      {fmt_mean(market_type, payload['implied_mean'])}")
    lines.append(f"  TRADING VOLUME    {fmt_volume(payload.get('market_volume_usd'))}")
    lines.append(f"  DAYS TO EVENT     {fmt_days(payload.get('days_to_event'))}")
    lines.append(f"  LAST RUN          {fmt_timestamp(payload.get('latest_run'))}")
    lines.append(f"  TREND             {trend_summary(market_type, payload['trend'])}")
    lines.append(f"  READINGS          {len(payload['trend'])} in last {TREND_DAYS} days")
    if payload.get("std_dev") is not None:
        lines.append(f"  STD DEV           {payload['std_dev']:.4f}")
    if payload.get("skewness") is not None:
        sk = payload["skewness"]
        skew_dir = "right-skewed" if sk > 0.1 else ("left-skewed" if sk < -0.1 else "symmetric")
        lines.append(f"  SKEWNESS          {sk:.4f}  ({skew_dir})")
    dist = payload.get("distribution") or []
    if dist:
        lines.append(f"  TOP OUTCOMES")
        for d in dist:
            prob_str = f"{(d['probability'] or 0) * 100:5.1f}%"
            lines.append(f"    {prob_str}  {d['label']}")


def get_formatted_report():
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

        if market_type == "FF_Rate":
            for meeting in m.get("meetings", []):
                _append_market_section(lines, "FF_Rate", m["label"], meeting)
        else:
            _append_market_section(lines, market_type, m["label"], m)

    acc = data.get("accuracy") or {}
    if acc:
        lines.append("")
        lines.append("  ACCURACY (vs FRED actuals)")
        lines.append(f"  {'─' * 40}")
        for market_type in ["CPI", "Core_CPI_MoM", "GDP", "Payrolls"]:
            if market_type not in acc:
                continue
            a = acc[market_type]
            paper = a.get("fed_paper_mae")
            paper_str = f"  (Fed paper: {paper})" if paper is not None else ""
            lines.append(f"  {a['label']:18s}  n={a['release_count']}  "
                         f"MAE={a['mae']:.4f}  RMSE={a['rmse']:.4f}{paper_str}")

    lines.append("")
    lines.append("=" * 60)
    return "\n".join(lines)


def push_to_github(local_path, commit_message=None):
    """
    Upload local_path to GITHUB_REPO at GITHUB_PATH on GITHUB_BRANCH using
    the GitHub Contents API. Reads GITHUB_TOKEN from the environment.

    Returns True on success, False on any failure (including missing token).
    Never raises — failures are logged so a bad push doesn't break the
    surrounding pipeline.
    """
    import base64
    import urllib.request
    import urllib.error

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("  GitHub push skipped: GITHUB_TOKEN not set.")
        return False

    if not os.path.exists(local_path):
        print(f"  GitHub push skipped: local file not found at {local_path}")
        return False

    with open(local_path, "rb") as f:
        content_bytes = f.read()
    content_b64 = base64.b64encode(content_bytes).decode("ascii")

    api_url = f"{GITHUB_API_BASE}/repos/{GITHUB_REPO}/contents/{GITHUB_PATH}"
    headers = {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":           "kalshi-monitor",
    }

    # Step 1: GET the file's current SHA (required to update an existing file).
    # If the file doesn't exist yet (first push), we'll create it without a SHA.
    existing_sha = None
    try:
        get_req = urllib.request.Request(
            f"{api_url}?ref={GITHUB_BRANCH}",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(get_req, timeout=GITHUB_TIMEOUT) as resp:
            existing = json.loads(resp.read().decode("utf-8"))
            existing_sha = existing.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # File doesn't exist yet — that's fine, we'll create it
            existing_sha = None
        else:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            print(f"  GitHub push failed (GET {e.code}): {e.reason}  {body}")
            return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  GitHub push failed (GET network): {e}")
        return False

    # Step 2: PUT the new content
    if not commit_message:
        commit_message = f"Update snapshot.json — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"

    payload = {
        "message": commit_message,
        "content": content_b64,
        "branch":  GITHUB_BRANCH,
    }
    if existing_sha:
        payload["sha"] = existing_sha

    try:
        put_req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="PUT",
        )
        with urllib.request.urlopen(put_req, timeout=GITHUB_TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            commit_sha = (result.get("commit") or {}).get("sha", "")[:7]
            action = "updated" if existing_sha else "created"
            print(f"  GitHub push OK: {action} {GITHUB_REPO}/{GITHUB_PATH} "
                  f"@ {GITHUB_BRANCH} (commit {commit_sha})")
            return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            pass
        print(f"  GitHub push failed (PUT {e.code}): {e.reason}  {body}")
        return False
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"  GitHub push failed (PUT network): {e}")
        return False


def write_snapshot(push=True):
    """
    Write the canonical snapshot.json to disk and (by default) push it to
    the configured public GitHub repo. If push=False, only writes locally.

    Also refreshes the local HTML dashboard at data/kalshi_dashboard.html
    by inlining the new snapshot into templates/kalshi_dashboard.html.
    Build failures (e.g. template missing) are logged but don't crash the
    caller — the snapshot has already been safely written by that point.
    """
    data = get_market_data()
    os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
    with open(SNAPSHOT_PATH, "w") as f:
        json.dump(data, f, indent=2)
    print(f"  Snapshot written to {SNAPSHOT_PATH}")

    # Refresh the local dashboard HTML so CoWork sees fresh data.
    # Always attempted on --write; isolated from snapshot write + GitHub push.
    try:
        import build_dashboard
        build_dashboard.build(
            template_path=build_dashboard.DEFAULT_TEMPLATE,
            snapshot_path=SNAPSHOT_PATH,
            output_path=build_dashboard.DEFAULT_OUTPUT,
        )
    except Exception as e:
        import traceback
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  [ERROR] {ts} | build_dashboard | {e}")
        traceback.print_exc()

    if push:
        gen = data.get("generated_at", "")
        commit_message = f"Update snapshot.json — {gen}"
        push_to_github(SNAPSHOT_PATH, commit_message=commit_message)

    return data


if __name__ == "__main__":
    if "--json" in sys.argv:
        print(json.dumps(get_market_data(), indent=2))
    elif "--push-only" in sys.argv:
        # Push the existing snapshot.json without regenerating it. Handy for
        # retrying a failed push without re-querying the DB.
        gen = ""
        try:
            with open(SNAPSHOT_PATH) as f:
                gen = json.load(f).get("generated_at", "")
        except Exception:
            pass
        push_to_github(SNAPSHOT_PATH, commit_message=f"Update snapshot.json — {gen}" if gen else None)
    elif "--write" in sys.argv:
        push = "--no-push" not in sys.argv
        write_snapshot(push=push)
    else:
        print(get_formatted_report())
