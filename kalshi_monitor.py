"""
Kalshi Macro Monitor — SQLite edition
Runs 4x daily via launchd (1am, 7am, 1pm, 7pm PT).
Fetches CPI, Core CPI, GDP, Recession, Fed Funds Rate, and Payrolls markets.
Writes to SQLite tables: contract_series, contracts, contract_details,
dashboard_summary.

Schema: kalshi_macro_data_v2 (see schema_v2.sql).
Snapshot/export is intentionally NOT called here — handle separately.
"""

import os
import sqlite3
import sys
import requests
from datetime import datetime, timezone

# Ensure imports find sibling scripts regardless of working directory
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config ────────────────────────────────────────────────────────────────────

KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY  = os.environ["KALSHI_API_KEY"]

DB_PATH = os.path.expanduser(
    os.environ.get("KALSHI_DB_PATH", "~/kalshi-monitor/db/kalshi_macro_data_v2.db")
)

# Each market:
#   market_type     - string matching markets.code in SQLite
#   series_ticker   - Kalshi series (used for date-encoded event lookup)
#   direct_event    - Kalshi event ticker to fetch directly (bypasses series lookup)
#   structure       - cumulative or mutually_exclusive
#   multi           - fetch multiple upcoming events (FF_Rate only)
MARKETS = [
    {"market_type": "CPI",          "series_ticker": "KXCPIYOY",      "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "Core_CPI_MoM", "series_ticker": "KXCPICORE",     "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "GDP",          "series_ticker": "KXGDP",         "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "Recession",    "series_ticker": None,            "direct_event": "KXNBERRECESSQ", "structure": "mutually_exclusive", "multi": False},
    {"market_type": "FF_Rate",      "series_ticker": "KXFEDDECISION", "direct_event": None,            "structure": "mutually_exclusive", "multi": True},
    {"market_type": "Payrolls",     "series_ticker": "KXPAYROLLS",    "direct_event": None,            "structure": "cumulative",         "multi": False},
]

# How many upcoming FOMC meetings to track
FF_RATE_MEETING_COUNT = 3


# ── Kalshi API ────────────────────────────────────────────────────────────────

def kalshi_headers():
    return {
        "Authorization": f"Bearer {KALSHI_API_KEY}",
        "Content-Type": "application/json",
    }


def get_active_event(series_ticker):
    """Return the single nearest upcoming active event for a series."""
    events = get_active_events(series_ticker, limit=1)
    return events[0] if events else None


def get_active_events(series_ticker, limit=None):
    """
    Return upcoming active events for a series, sorted nearest-first.
    If limit is set, return at most that many.
    """
    url = f"{KALSHI_BASE_URL}/events"
    params = {"series_ticker": series_ticker, "limit": 100}
    resp = requests.get(url, headers=kalshi_headers(), params=params)
    resp.raise_for_status()
    events = resp.json().get("events", [])

    if not events:
        return []

    month_order = {
        "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12
    }

    def ticker_sort_key(e):
        ticker = e.get("event_ticker", "")
        parts = ticker.split("-")
        if len(parts) < 2:
            return (99, 99, 99)
        suffix = parts[-1]
        try:
            year = int(suffix[:2])
            month_str = suffix[2:5]
            month = month_order.get(month_str, 99)
            day = int(suffix[5:]) if len(suffix) > 5 else 1
            return (year, month, day)
        except (ValueError, IndexError):
            return (99, 99, 99)

    events_sorted = sorted(events, key=ticker_sort_key)

    active_events = []
    for event in events_sorted:
        event_ticker = event["event_ticker"]
        try:
            full_data = get_event_with_markets(event_ticker)
            markets = full_data.get("markets", [])
            if any(m.get("status") == "active" for m in markets):
                active_events.append(full_data.get("event", event))
                if limit and len(active_events) >= limit:
                    break
        except Exception as e:
            print(f"  Skipping {event_ticker}: {e}")
            continue

    return active_events


def get_event_with_markets(event_ticker):
    """Fetch a single event including its markets array."""
    url = f"{KALSHI_BASE_URL}/events/{event_ticker}"
    resp = requests.get(url, headers=kalshi_headers())
    resp.raise_for_status()
    return resp.json()


# ── Distribution Math ─────────────────────────────────────────────────────────

def _market_yes_price(m):
    """Return the 'yes' price for a market as a float in [0, 1]."""
    lp = m.get("last_price_dollars")
    if isinstance(lp, str):
        try:
            return float(lp)
        except ValueError:
            return 0.0
    # last_price is in cents (0-100)
    return (m.get("last_price") or 0) / 100.0


def parse_cumulative_distribution(markets):
    """
    For CPI, Core CPI, GDP, Payrolls: contracts are cumulative 'above X' strikes.
    Returns (bin_rows, implied_mean, std_dev, skewness).

    bin_rows is a list of dicts, each describing one *real* Kalshi sub-market:
      {ticker, strike, yes_price, bin_prob, volume}
    No synthetic below-lowest bin is included (that was an Airtable artifact);
    however, it IS used internally for the moment calculation so the
    distribution sums to 1.
    """
    valid = [
        m for m in markets
        if m.get("floor_strike") is not None and m.get("status") == "active"
    ]
    if not valid:
        return [], None, None, None

    # If all prices are zero the market has no trading activity yet — skip
    if all(_market_yes_price(m) == 0 for m in valid):
        print(f"  Skipping — no trading activity yet (all prices zero).")
        return [], None, None, None

    valid.sort(key=lambda m: m["floor_strike"])

    # Step 1: build cumulative-above table
    cum = []
    for m in valid:
        cum.append({
            "ticker":         m.get("ticker"),
            "strike":         m["floor_strike"],
            "yes_price":      _market_yes_price(m),
            "volume":         int(m.get("volume", 0) or 0),
        })

    # Step 2: derive per-bin probabilities (cum[i] - cum[i+1]; top tail = cum[-1])
    bin_rows = []
    for i, c in enumerate(cum):
        if i < len(cum) - 1:
            bin_prob = c["yes_price"] - cum[i + 1]["yes_price"]
        else:
            bin_prob = c["yes_price"]
        bin_rows.append({
            "ticker":    c["ticker"],
            "strike":    c["strike"],
            "yes_price": round(c["yes_price"], 4),
            "bin_prob":  round(bin_prob, 4),
            "volume":    c["volume"],
        })

    # Step 3: for moment math, prepend the implied below-lowest bin
    below_prob = 1.0 - cum[0]["yes_price"]
    interval = (cum[1]["strike"] - cum[0]["strike"]) if len(cum) > 1 else 0.1

    # Build (midpoint, prob) pairs across all bins (including below-lowest)
    strikes_for_moments = [cum[0]["strike"] - interval] + [r["strike"] for r in bin_rows]
    probs_for_moments   = [below_prob] + [r["bin_prob"] for r in bin_rows]

    midpoints = []
    for i, s in enumerate(strikes_for_moments):
        if i < len(strikes_for_moments) - 1:
            midpoints.append((s + strikes_for_moments[i + 1]) / 2)
        else:
            midpoints.append(s)

    # First moment: implied mean
    implied_mean = sum(m * p for m, p in zip(midpoints, probs_for_moments))

    # Standard deviation (stored in same units as underlying — see project notes
    # on Payrolls)
    variance = sum(p * (m - implied_mean) ** 2 for m, p in zip(midpoints, probs_for_moments))
    std_dev = variance ** 0.5

    # Third standardized moment: skewness
    if std_dev > 0:
        skewness = sum(p * ((m - implied_mean) / std_dev) ** 3
                       for m, p in zip(midpoints, probs_for_moments))
    else:
        skewness = 0.0

    return (
        bin_rows,
        round(implied_mean, 4),
        round(std_dev, 4),
        round(skewness, 4),
    )


def parse_mutually_exclusive(markets):
    """
    For FF Rate and Recession: each market is a discrete outcome bucket.
    Returns (rows, modal_prob). std_dev/skewness are NULL for these markets.

    rows is a list of dicts:
      {ticker, label, yes_price, prob, volume}
    """
    active = [m for m in markets if m.get("status") == "active"]
    rows = []
    for m in active:
        label = m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker", "")
        yes_price = _market_yes_price(m)
        rows.append({
            "ticker":    m.get("ticker"),
            "label":     label,
            "yes_price": round(yes_price, 4),
            "prob":      round(yes_price, 4),  # for ME markets, yes price == probability
            "volume":    int(m.get("volume", 0) or 0),
        })

    modal_prob = max((r["prob"] for r in rows), default=0.0)
    return rows, round(modal_prob, 4)


# ── Days to Event ─────────────────────────────────────────────────────────────

def days_to_event(event, markets=None):
    """Calculate days from now to the event."""
    date_str = event.get("strike_date") or ""
    if date_str and not date_str.startswith("0001"):
        try:
            strike_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return max(0, (strike_dt - now).days)
        except Exception:
            pass

    if markets:
        for m in markets:
            exp = m.get("expected_expiration_time") or ""
            if exp and not exp.startswith("0001"):
                try:
                    exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    return max(0, (exp_dt - now).days)
                except Exception:
                    continue
    return None


def get_market_volume_usd(markets):
    """
    Return total dollar volume for the event by summing volume_fp
    (string decimal) across all markets.
    """
    total = sum(float(m.get("volume_fp") or 0) for m in markets)
    return int(total) if total > 0 else None


# ── SQLite plumbing ───────────────────────────────────────────────────────────

def open_db():
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(
            f"SQLite database not found at {DB_PATH}. "
            f"Create with schema_v2.sql first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def load_market_lookup(conn):
    """Return {market_code: market_id}."""
    cur = conn.cursor()
    cur.execute("SELECT code, id FROM markets;")
    return dict(cur.fetchall())


def get_or_create_contract_series(conn, series_code, market_id, strike_structure):
    """Return contract_series.id, inserting if missing."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO contract_series (code, market_id, strike_structure)
        VALUES (?, ?, ?)
        ON CONFLICT(code) DO UPDATE SET
            market_id = excluded.market_id,
            strike_structure = COALESCE(excluded.strike_structure, contract_series.strike_structure);
    """, (series_code, market_id, strike_structure))
    cur.execute("SELECT id FROM contract_series WHERE code = ?;", (series_code,))
    return cur.fetchone()[0]


def get_or_create_contract(conn, series_id, contract_code, strike, label):
    """Return contracts.id, inserting if missing. is_synthetic=0 for live data."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO contracts (contract_series_id, code, strike, label, is_synthetic)
        VALUES (?, ?, ?, ?, 0)
        ON CONFLICT(code) DO UPDATE SET
            strike = COALESCE(excluded.strike, contracts.strike),
            label  = COALESCE(excluded.label,  contracts.label);
    """, (series_id, contract_code, strike, label))
    cur.execute("SELECT id FROM contracts WHERE code = ?;", (contract_code,))
    return cur.fetchone()[0]


def write_contract_detail(conn, contract_id, timestamp, yes_price,
                          implied_probability, volume, days, market_volume_usd):
    """Insert one snapshot row for one contract."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO contract_details
            (contract_id, timestamp, yes_price, implied_probability,
             volume, days_to_event, market_volume_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?);
    """, (
        contract_id,
        timestamp,
        yes_price,
        implied_probability,
        int(volume) if volume is not None else None,
        days if days is not None else 0,
        market_volume_usd,
    ))


def write_dashboard_summary(conn, series_id, timestamp, implied_mean, days,
                            market_volume_usd, std_dev, skewness):
    """Upsert one summary row for the event-snapshot."""
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO dashboard_summary
            (contract_series_id, timestamp, implied_mean, days_to_event,
             market_volume_usd, std_dev, skewness)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_series_id, timestamp) DO UPDATE SET
            implied_mean      = excluded.implied_mean,
            days_to_event     = excluded.days_to_event,
            market_volume_usd = excluded.market_volume_usd,
            std_dev           = excluded.std_dev,
            skewness          = excluded.skewness;
    """, (
        series_id,
        timestamp,
        implied_mean,
        days if days is not None else 0,
        market_volume_usd,
        std_dev,
        skewness,
    ))


# ── Main per-event processing ─────────────────────────────────────────────────

def process_event(conn, event, market_type, structure, market_id, timestamp):
    """Process a single Kalshi event and write snapshot rows to SQLite."""
    event_ticker = event["event_ticker"]
    print(f"  Active event: {event_ticker}")

    full_data = get_event_with_markets(event_ticker)
    markets = full_data.get("markets", [])
    event_meta = full_data.get("event", event)
    days = days_to_event(event_meta, markets)
    market_volume_usd = get_market_volume_usd(markets)

    std_dev = None
    skewness = None

    if structure == "mutually_exclusive":
        rows, implied_mean = parse_mutually_exclusive(markets)
    else:
        rows, implied_mean, std_dev, skewness = parse_cumulative_distribution(markets)

    if not rows:
        print(f"  No valid market data found, skipping.")
        return

    # Upsert series, then each contract, then snapshot rows
    series_id = get_or_create_contract_series(conn, event_ticker, market_id, structure)

    contract_rows_written = 0
    for r in rows:
        contract_code = r["ticker"]
        if not contract_code:
            # Without a sub-market ticker we can't safely create a stable
            # contract row — skip and report at the end
            continue

        if structure == "cumulative":
            strike = r["strike"]
            label = None  # label can be populated later from market metadata
            implied_probability = r["bin_prob"]
            yes_price = r["yes_price"]
        else:
            strike = None
            label = r.get("label")
            implied_probability = r["prob"]
            yes_price = r["yes_price"]

        contract_id = get_or_create_contract(conn, series_id, contract_code, strike, label)
        write_contract_detail(
            conn,
            contract_id=contract_id,
            timestamp=timestamp,
            yes_price=yes_price,
            implied_probability=implied_probability,
            volume=r["volume"],
            days=days,
            market_volume_usd=market_volume_usd,
        )
        contract_rows_written += 1

    write_dashboard_summary(
        conn,
        series_id=series_id,
        timestamp=timestamp,
        implied_mean=implied_mean,
        days=days,
        market_volume_usd=market_volume_usd,
        std_dev=std_dev,
        skewness=skewness,
    )

    conn.commit()

    vol_str  = f"${market_volume_usd:,}" if market_volume_usd is not None else "n/a"
    std_str  = f"{std_dev:.4f}" if std_dev is not None else "n/a"
    skew_str = f"{skewness:.4f}" if skewness is not None else "n/a"
    print(f"  Wrote {contract_rows_written} contract rows + summary: "
          f"{market_type} | mean={implied_mean} | std={std_str} | "
          f"skew={skew_str} | days={days} | vol={vol_str}")


def log_error(market_type, message, exc=None):
    """Print a structured error line with timestamp."""
    import traceback
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  [ERROR] {ts} | {market_type} | {message}")
    if exc:
        traceback.print_exc()


def run(skip_export=False):
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"Kalshi Monitor run: {timestamp}")
    print(f"DB: {DB_PATH}")
    print(f"{'='*60}")

    conn = open_db()
    market_ids = load_market_lookup(conn)
    if not market_ids:
        print("ERROR: markets table is empty. Run schema_v2.sql first.")
        conn.close()
        sys.exit(1)

    results = {}  # market_type -> "ok" | "failed" | "skipped"

    try:
        for market in MARKETS:
            market_type   = market["market_type"]
            series_ticker = market["series_ticker"]
            direct_event  = market["direct_event"]
            structure     = market["structure"]
            multi         = market["multi"]

            market_id = market_ids.get(market_type)
            if market_id is None:
                log_error(market_type, f"unknown market_type — not in markets table")
                results[market_type] = "failed"
                continue

            print(f"\n[{market_type}] Fetching active event(s)...")
            try:
                if direct_event:
                    full_data = get_event_with_markets(direct_event)
                    event = full_data.get("event", {"event_ticker": direct_event})
                    process_event(conn, event, market_type, structure, market_id, timestamp)

                elif multi:
                    events = get_active_events(series_ticker, limit=FF_RATE_MEETING_COUNT)
                    if not events:
                        print(f"  No active events found.")
                        results[market_type] = "skipped"
                        continue
                    for event in events:
                        process_event(conn, event, market_type, structure, market_id, timestamp)

                else:
                    event = get_active_event(series_ticker)
                    if not event:
                        print(f"  No active event found.")
                        results[market_type] = "skipped"
                        continue
                    process_event(conn, event, market_type, structure, market_id, timestamp)

                results[market_type] = "ok"

            except Exception as e:
                log_error(market_type, str(e), exc=e)
                results[market_type] = "failed"
    finally:
        conn.close()

    # ── Run summary ───────────────────────────────────────────────────────────
    ok      = [k for k, v in results.items() if v == "ok"]
    failed  = [k for k, v in results.items() if v == "failed"]
    skipped = [k for k, v in results.items() if v == "skipped"]

    print(f"\n{'='*60}")
    print(f"Run complete: {len(ok)} OK, {len(failed)} failed, {len(skipped)} skipped")
    if failed:
        print(f"  Failed:  {', '.join(failed)}")
    if skipped:
        print(f"  Skipped: {', '.join(skipped)}")
    print(f"{'='*60}\n")

    # ── Export + push ─────────────────────────────────────────────────────────
    # Only fire if at least one market succeeded — no point publishing a stale
    # snapshot when the whole run flopped. Errors here never crash the monitor;
    # the data is already safely committed to SQLite above.
    if skip_export:
        print("Export skipped (--no-export).")
        return
    if not ok:
        print("Export skipped: no markets succeeded this run.")
        return

    print(f"{'='*60}")
    print("Generating snapshot and pushing to GitHub...")
    print(f"{'='*60}")
    try:
        import kalshi_export
        kalshi_export.write_snapshot(push=True)
    except Exception as e:
        import traceback
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"  [ERROR] {ts} | export | {e}")
        traceback.print_exc()


if __name__ == "__main__":
    skip_export = "--no-export" in sys.argv
    run(skip_export=skip_export)
