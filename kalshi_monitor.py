"""
Kalshi Macro Monitor
Runs 3x daily (1am, noon, 5pm PT) via cron.
Fetches CPI, Core CPI, GDP, Recession, and Fed Funds Rate markets.
Writes to Airtable Contract_Details and Dashboard_Summary tables.
"""

import os
import requests
from datetime import datetime, timezone
from pyairtable import Api
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ── Config ────────────────────────────────────────────────────────────────────

KALSHI_BASE_URL  = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_API_KEY   = os.environ["KALSHI_API_KEY"]
AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

# Each market:
#   market_type     - string written to Airtable
#   series_ticker   - Kalshi series (used for date-encoded event lookup)
#   direct_event    - Kalshi event ticker to fetch directly (bypasses series lookup)
#   structure       - cumulative or mutually_exclusive
#   multi           - fetch multiple upcoming events (FF_Rate only)
MARKETS = [
    {"market_type": "CPI",       "series_ticker": "KXCPIYOY",      "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "Core_CPI_MoM", "series_ticker": "KXCPICORE",     "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "GDP",       "series_ticker": "KXGDP",         "direct_event": None,            "structure": "cumulative",         "multi": False},
    {"market_type": "Recession", "series_ticker": None,            "direct_event": "KXNBERRECESSQ", "structure": "mutually_exclusive", "multi": False},
    {"market_type": "FF_Rate",   "series_ticker": "KXFEDDECISION", "direct_event": None,            "structure": "mutually_exclusive", "multi": True},
    {"market_type": "Payrolls",  "series_ticker": "KXPAYROLLS",     "direct_event": None,            "structure": "cumulative",         "multi": False},
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

def parse_cumulative_distribution(markets):
    """
    For CPI, Core CPI, GDP: contracts are cumulative 'above X' strikes.
    Returns list of (strike, bin_prob, volume) tuples plus implied_mean.
    """
    valid = [
        m for m in markets
        if m.get("floor_strike") is not None and m.get("status") == "active"
    ]
    if not valid:
        return [], None

    # If all prices are zero the market has no trading activity yet — skip
    if all(float(m.get("last_price_dollars") or 0) == 0 and m.get("last_price", 0) == 0 for m in valid):
        print(f"  Skipping — no trading activity yet (all prices zero).")
        return [], None

    valid.sort(key=lambda m: m["floor_strike"])

    bins = []
    for m in valid:
        bins.append({
            "strike": m["floor_strike"],
            "cum_prob_above": float(m.get("last_price_dollars") or m.get("last_price", 0) or 0) if isinstance(m.get("last_price_dollars"), str) else m.get("last_price", 0) / 100.0,
            "volume": m.get("volume", 0),
        })

    bin_results = []
    for i in range(len(bins)):
        if i < len(bins) - 1:
            bin_prob = bins[i]["cum_prob_above"] - bins[i + 1]["cum_prob_above"]
        else:
            bin_prob = bins[i]["cum_prob_above"]  # top tail
        bin_results.append((bins[i]["strike"], round(bin_prob, 4), bins[i]["volume"]))

    # Below-lowest bin
    below_prob = 1.0 - bins[0]["cum_prob_above"]
    lowest_strike = bins[0]["strike"]
    interval = (bins[1]["strike"] - bins[0]["strike"]) if len(bins) > 1 else 0.1
    bin_results.insert(0, (round(lowest_strike - interval, 4), round(below_prob, 4), 0))

    # Implied mean
    implied_mean = 0.0
    for i, (strike, prob, _) in enumerate(bin_results):
        if i < len(bin_results) - 1:
            midpoint = (strike + bin_results[i + 1][0]) / 2
        else:
            midpoint = strike
        implied_mean += midpoint * prob

    return bin_results, round(implied_mean, 4)


def parse_mutually_exclusive(markets):
    """
    For FF Rate and Recession: each market is a discrete outcome bucket.
    Returns list of (label, prob, volume) tuples plus the modal probability
    (probability of the single most likely outcome).
    """
    active = [m for m in markets if m.get("status") == "active"]
    results = []
    for m in active:
        # Use yes_sub_title for recession quarters, subtitle/ticker otherwise
        label = m.get("yes_sub_title") or m.get("subtitle") or m.get("ticker", "")
        lp = m.get("last_price_dollars") or m.get("last_price")
        prob = float(lp) if isinstance(lp, str) else (lp or 0) / 100.0
        volume = m.get("volume", 0)
        results.append((label, round(prob, 4), volume))

    # implied_mean = probability of the modal outcome (most likely single bucket)
    # e.g. 0.90 means "90% chance of the most likely outcome"
    # Much more interpretable than bps expected value on the dashboard
    modal_prob = max((prob for _, prob, _ in results), default=0.0)
    return results, round(modal_prob, 4)


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


# ── Airtable ──────────────────────────────────────────────────────────────────

def get_airtable_tables():
    api = Api(AIRTABLE_API_KEY)
    base = api.base(AIRTABLE_BASE_ID)
    return base.table("Contract_Details"), base.table("Dashboard_Summary")


def get_market_volume_usd(event_meta, markets):
    """
    Return total dollar volume for the event by summing volume_fp
    (string decimal) across all markets.
    """
    # volume_fp is a string decimal on each market (e.g. "3910875.00")
    total = sum(float(m.get("volume_fp") or 0) for m in markets)
    return int(total) if total > 0 else None


def write_contract_details(table, timestamp, market_type, contract_series, structure, bins, days, market_volume_usd):
    """Write one row per strike to Contract_Details."""
    records = []
    for strike, prob, volume in bins:
        numeric_strike = strike if isinstance(strike, (int, float)) else None
        record = {
            "timestamp": timestamp,
            "market_type": market_type,
            "contract_series": contract_series,
            "strike_structure": structure,
            "strike": numeric_strike,
            "implied_probability": prob,
            "volume": int(volume),
            "days_to_event": days if days is not None else 0,
        }
        if market_volume_usd is not None:
            record["market_volume_usd"] = market_volume_usd
        records.append(record)
    if records:
        table.batch_create(records)
        print(f"  Wrote {len(records)} contract rows for {contract_series}")


def write_dashboard_summary(table, timestamp, market_type, contract_series, structure, implied_mean, days, market_volume_usd):
    record = {
        "timestamp": timestamp,
        "market_type": market_type,
        "contract_series": contract_series,
        "strike_structure": structure,
        "implied_mean": implied_mean,
        "days_to_event": days if days is not None else 0,
    }
    if market_volume_usd is not None:
        record["market_volume_usd"] = market_volume_usd
    table.create(record)
    vol_str = f"${market_volume_usd:,}" if market_volume_usd is not None else "n/a"
    print(f"  Wrote dashboard summary: {market_type} | mean={implied_mean} | days={days} | vol={vol_str}")


# ── Main ──────────────────────────────────────────────────────────────────────

def process_event(event, market_type, structure, contract_table, summary_table, timestamp):
    """Process a single event and write to Airtable."""
    event_ticker = event["event_ticker"]
    print(f"  Active event: {event_ticker}")

    full_data = get_event_with_markets(event_ticker)
    markets = full_data.get("markets", [])
    event_meta = full_data.get("event", event)
    days = days_to_event(event_meta, markets)
    market_volume_usd = get_market_volume_usd(event_meta, markets)

    if structure == "mutually_exclusive":
        bins, implied_mean = parse_mutually_exclusive(markets)
    else:
        bins, implied_mean = parse_cumulative_distribution(markets)

    if not bins:
        print(f"  No valid market data found, skipping.")
        return

    write_contract_details(contract_table, timestamp, market_type, event_ticker, structure, bins, days, market_volume_usd)
    write_dashboard_summary(summary_table, timestamp, market_type, event_ticker, structure, implied_mean, days, market_volume_usd)


def log_error(market_type, message, exc=None):
    """Print a structured error line with timestamp."""
    import traceback
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  [ERROR] {ts} | {market_type} | {message}")
    if exc:
        traceback.print_exc()


def run():
    timestamp = datetime.now(timezone.utc).isoformat()
    print(f"\n{'='*60}")
    print(f"Kalshi Monitor run: {timestamp}")
    print(f"{'='*60}")

    contract_table, summary_table = get_airtable_tables()

    results = {}  # market_type -> "ok" | "failed" | "skipped"

    for market in MARKETS:
        market_type   = market["market_type"]
        series_ticker = market["series_ticker"]
        direct_event  = market["direct_event"]
        structure     = market["structure"]
        multi         = market["multi"]

        print(f"\n[{market_type}] Fetching active event(s)...")
        try:
            if direct_event:
                full_data = get_event_with_markets(direct_event)
                event = full_data.get("event", {"event_ticker": direct_event})
                process_event(event, market_type, structure, contract_table, summary_table, timestamp)

            elif multi:
                events = get_active_events(series_ticker, limit=FF_RATE_MEETING_COUNT)
                if not events:
                    print(f"  No active events found.")
                    results[market_type] = "skipped"
                    continue
                for event in events:
                    process_event(event, market_type, structure, contract_table, summary_table, timestamp)

            else:
                event = get_active_event(series_ticker)
                if not event:
                    print(f"  No active event found.")
                    results[market_type] = "skipped"
                    continue
                process_event(event, market_type, structure, contract_table, summary_table, timestamp)

            results[market_type] = "ok"

        except Exception as e:
            log_error(market_type, str(e), exc=e)
            results[market_type] = "failed"

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

    # ── Write CoWork snapshot ─────────────────────────────────────────────────
    try:
        from kalshi_export import write_snapshot
        write_snapshot()
    except Exception as e:
        log_error("snapshot", str(e), exc=e)


if __name__ == "__main__":
    run()
