"""
Kalshi ETL v2 — Airtable → SQLite (with contracts layer)

Differences from v1:
  * Builds the `contracts` table between `contract_series` and `contract_details`
  * For cumulative markets, derives one contract per (event, strike) pair
  * For ME markets (FF_Rate, Recession), creates one synthetic "bucket" contract
    per event with code F-{event}-HISTORICAL; all ME contract_details rows
    attach to it (positions are random in source data; we verified this)
  * `notable_event` no longer migrated (column removed in v2 schema)
  * No UNIQUE index on (contract_id, timestamp) for contract_details — multiple
    rows at the same snapshot for the synthetic bucket are expected and kept

Usage:
    python3 etl_airtable_to_sqlite_v2.py [--db PATH] [--truncate] [--dry-run]
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

from pyairtable import Api

# ── Config ────────────────────────────────────────────────────────────────────

AIRTABLE_API_KEY = os.environ["AIRTABLE_API_KEY"]
AIRTABLE_BASE_ID = os.environ["AIRTABLE_BASE_ID"]

DEFAULT_DB_PATH = os.path.expanduser("~/kalshi-monitor/db/kalshi_macro_data_v2.db")

# Markets where strike is meaningful — every other market gets a synthetic bucket
CUMULATIVE_MARKETS = {"CPI", "Core_CPI_MoM", "GDP", "Payrolls"}
ME_MARKETS = {"FF_Rate", "Recession"}

PROGRESS_EVERY = 2000


# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_table(api, table_name):
    print(f"  Fetching {table_name}...", end="", flush=True)
    table = api.base(AIRTABLE_BASE_ID).table(table_name)
    records = table.all()
    rows = [r.get("fields", {}) for r in records]
    print(f" {len(rows):,} rows")
    return rows


# ── Truncate ──────────────────────────────────────────────────────────────────

def truncate_data(conn):
    """
    Delete data rows in reverse FK dependency order. Preserves `markets`.
    """
    print("Truncating data tables (preserving markets)...")
    cur = conn.cursor()
    for table in [
        "event_annotations",
        "accuracy_log",
        "dashboard_summary",
        "contract_details",
        "contracts",
        "contract_series",
    ]:
        cur.execute(f"DELETE FROM {table};")
        cur.execute(f"DELETE FROM sqlite_sequence WHERE name = '{table}';")
        print(f"  {table}: cleared")
    conn.commit()


# ── Lookups ───────────────────────────────────────────────────────────────────

def load_market_lookup(conn):
    cur = conn.cursor()
    cur.execute("SELECT code, id FROM markets;")
    return dict(cur.fetchall())


# ── Phase: contract_series ────────────────────────────────────────────────────

def derive_and_upsert_contract_series(conn, market_ids, contract_details, dashboard_summary, accuracy_log):
    """
    Same as v1: derive series from union of source tables, upsert.
    Returns {series_code: series_id}.
    """
    print("Deriving contract_series from source tables...")
    series = {}

    def record_series(row, source_name):
        code = row.get("contract_series")
        market = row.get("market_type")
        if not code or not market:
            return
        if code not in series:
            series[code] = {"market_type": market, "strike_structure": None}
        ss = row.get("strike_structure")
        if ss and series[code]["strike_structure"] is None:
            series[code]["strike_structure"] = ss
        if series[code]["market_type"] != market:
            print(f"  WARNING: {code} appears under both "
                  f"{series[code]['market_type']} and {market} in {source_name}")

    for row in contract_details:    record_series(row, "Contract_Details")
    for row in dashboard_summary:   record_series(row, "Dashboard_Summary")
    for row in accuracy_log:        record_series(row, "Accuracy_Log")

    print(f"  {len(series)} unique series found")

    cur = conn.cursor()
    for code, meta in series.items():
        market_id = market_ids.get(meta["market_type"])
        if market_id is None:
            print(f"  ERROR: unknown market_type '{meta['market_type']}' for series {code}")
            sys.exit(1)
        cur.execute("""
            INSERT INTO contract_series (code, market_id, strike_structure)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                market_id = excluded.market_id,
                strike_structure = COALESCE(excluded.strike_structure, contract_series.strike_structure);
        """, (code, market_id, meta["strike_structure"]))
    conn.commit()
    print(f"  {len(series)} series upserted")

    cur.execute("SELECT code, id FROM contract_series;")
    return dict(cur.fetchall())


# ── Phase: contracts ──────────────────────────────────────────────────────────

def derive_and_upsert_contracts(conn, market_ids, series_ids, contract_details):
    """
    Build the contracts table from Contract_Details rows.

    For cumulative markets:
        - One contract per (series, strike) — real contracts
        - Code: {series}-S{strike} (e.g., KXCPIYOY-26MAR-S2.5)
        - is_synthetic = 0

    For ME markets:
        - One contract per series — synthetic bucket
        - Code: F-{series}-HISTORICAL
        - strike = NULL
        - is_synthetic = 1

    Returns two lookups:
        cumulative_contracts: {(series_id, strike): contract_id}
        bucket_contracts:     {series_id: contract_id}
    """
    print("Deriving contracts from Contract_Details...")
    # Reverse: id → market_code, for classifying
    market_by_id = {v: k for k, v in market_ids.items()}

    # Need to know which series belong to which market type
    cur = conn.cursor()
    cur.execute("SELECT id, code, market_id FROM contract_series;")
    series_meta = {row[0]: {"code": row[1], "market_id": row[2]} for row in cur.fetchall()}

    cumulative_contracts = {}    # (series_id, strike) -> contract_id
    bucket_contracts = {}        # series_id -> contract_id

    # Pass 1: collect distinct (series, strike) for cumulative markets,
    # and identify which series need synthetic buckets
    cumulative_seen = set()      # (series_id, strike)
    me_series_seen = set()       # series_id

    for row in contract_details:
        series_code = row.get("contract_series")
        market = row.get("market_type")
        series_id = series_ids.get(series_code)
        if series_id is None:
            continue

        if market in CUMULATIVE_MARKETS:
            strike = row.get("strike")
            if strike is None:
                # Cumulative market with null strike — shouldn't happen but
                # skip to be safe
                continue
            cumulative_seen.add((series_id, strike))
        elif market in ME_MARKETS:
            me_series_seen.add(series_id)

    print(f"  {len(cumulative_seen)} cumulative (series, strike) pairs")
    print(f"  {len(me_series_seen)} ME series → 1 synthetic bucket each")

    # Pass 2: upsert
    # Cumulative contracts
    for series_id, strike in cumulative_seen:
        series_code = series_meta[series_id]["code"]
        contract_code = f"{series_code}-S{strike}"
        cur.execute("""
            INSERT INTO contracts (contract_series_id, code, strike, is_synthetic)
            VALUES (?, ?, ?, 0)
            ON CONFLICT(code) DO UPDATE SET
                strike = excluded.strike;
        """, (series_id, contract_code, strike))
        cumulative_contracts[(series_id, strike)] = cur.lastrowid or None

    # Synthetic buckets for ME markets
    for series_id in me_series_seen:
        series_code = series_meta[series_id]["code"]
        contract_code = f"F-{series_code}-HISTORICAL"
        cur.execute("""
            INSERT INTO contracts (contract_series_id, code, strike, is_synthetic)
            VALUES (?, ?, NULL, 1)
            ON CONFLICT(code) DO UPDATE SET
                is_synthetic = 1;
        """, (series_id, contract_code))
        bucket_contracts[series_id] = cur.lastrowid or None

    conn.commit()

    # Reload to get all IDs reliably (lastrowid is None on ON CONFLICT UPDATE path)
    cur.execute("""
        SELECT id, contract_series_id, strike, code, is_synthetic
        FROM contracts;
    """)
    cumulative_contracts.clear()
    bucket_contracts.clear()
    for cid, sid, strike, code, syn in cur.fetchall():
        if syn:
            bucket_contracts[sid] = cid
        else:
            cumulative_contracts[(sid, strike)] = cid

    total = len(cumulative_contracts) + len(bucket_contracts)
    print(f"  {total} contracts upserted "
          f"({len(cumulative_contracts)} real, {len(bucket_contracts)} synthetic)")

    return cumulative_contracts, bucket_contracts


# ── Phase: contract_details ───────────────────────────────────────────────────

def load_contract_details(conn, rows, series_ids, cumulative_contracts, bucket_contracts):
    """
    Insert contract_details rows (not upsert — no unique constraint, since
    synthetic buckets need to accept many rows per timestamp).

    Routes each Airtable row to the right contract:
      - cumulative market with strike → real contract by (series, strike)
      - ME market → synthetic bucket for that series
    """
    print(f"Loading contract_details ({len(rows):,} rows)...")
    cur = conn.cursor()
    skipped_no_series = 0
    skipped_no_contract = 0
    inserted = 0
    batch = []

    for row in rows:
        series_id = series_ids.get(row.get("contract_series"))
        if series_id is None:
            skipped_no_series += 1
            continue

        market = row.get("market_type")
        contract_id = None
        if market in CUMULATIVE_MARKETS:
            strike = row.get("strike")
            if strike is not None:
                contract_id = cumulative_contracts.get((series_id, strike))
        elif market in ME_MARKETS:
            contract_id = bucket_contracts.get(series_id)

        if contract_id is None:
            skipped_no_contract += 1
            continue

        batch.append((
            contract_id,
            row.get("timestamp"),
            row.get("yes_price"),
            row.get("implied_probability"),
            row.get("volume"),
            row.get("days_to_event"),
            row.get("market_volume_usd"),
        ))

        if len(batch) >= 1000:
            cur.executemany("""
                INSERT INTO contract_details
                    (contract_id, timestamp, yes_price, implied_probability,
                     volume, days_to_event, market_volume_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?);
            """, batch)
            inserted += len(batch)
            batch = []
            if inserted % PROGRESS_EVERY == 0:
                print(f"  {inserted:,} / {len(rows):,}")

    if batch:
        cur.executemany("""
            INSERT INTO contract_details
                (contract_id, timestamp, yes_price, implied_probability,
                 volume, days_to_event, market_volume_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?);
        """, batch)
        inserted += len(batch)

    conn.commit()
    print(f"  done: {inserted:,} inserted, "
          f"{skipped_no_series} skipped (no series), "
          f"{skipped_no_contract} skipped (no contract)")


# ── Phase: dashboard_summary ──────────────────────────────────────────────────

def load_dashboard_summary(conn, rows, series_ids):
    """notable_event no longer migrated — column dropped in v2."""
    print(f"Loading dashboard_summary ({len(rows):,} rows)...")
    cur = conn.cursor()
    skipped = 0
    batch = []

    for row in rows:
        series_id = series_ids.get(row.get("contract_series"))
        if series_id is None:
            skipped += 1
            continue
        batch.append((
            series_id,
            row.get("timestamp"),
            row.get("implied_mean"),
            row.get("days_to_event"),
            row.get("market_volume_usd"),
            row.get("std_dev"),
            row.get("skewness"),
        ))

    cur.executemany("""
        INSERT INTO dashboard_summary
            (contract_series_id, timestamp, implied_mean, days_to_event,
             market_volume_usd, std_dev, skewness)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(contract_series_id, timestamp) DO UPDATE SET
            implied_mean = excluded.implied_mean,
            days_to_event = excluded.days_to_event,
            market_volume_usd = excluded.market_volume_usd,
            std_dev = excluded.std_dev,
            skewness = excluded.skewness;
    """, batch)
    conn.commit()
    print(f"  done: {len(batch):,} upserted, {skipped} skipped")


# ── Phase: accuracy_log ───────────────────────────────────────────────────────

def load_accuracy_log(conn, rows, market_ids, series_ids):
    print(f"Loading accuracy_log ({len(rows):,} rows)...")
    cur = conn.cursor()
    skipped = 0
    batch = []

    for row in rows:
        market_id = market_ids.get(row.get("market_type"))
        series_id = series_ids.get(row.get("contract_series"))
        if market_id is None or series_id is None:
            skipped += 1
            continue
        batch.append((
            market_id,
            series_id,
            row.get("run_date"),
            row.get("release_date"),
            row.get("kalshi_implied_mean"),
            row.get("actual_value"),
            row.get("error"),
            row.get("abs_error"),
            row.get("days_before"),
            row.get("readings_in_window"),
        ))

    cur.executemany("""
        INSERT INTO accuracy_log
            (market_id, contract_series_id, run_date, release_date,
             kalshi_implied_mean, actual_value, error, abs_error,
             days_before, readings_in_window)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(market_id, release_date) DO UPDATE SET
            contract_series_id = excluded.contract_series_id,
            run_date = excluded.run_date,
            kalshi_implied_mean = excluded.kalshi_implied_mean,
            actual_value = excluded.actual_value,
            error = excluded.error,
            abs_error = excluded.abs_error,
            days_before = excluded.days_before,
            readings_in_window = excluded.readings_in_window;
    """, batch)
    conn.commit()
    print(f"  done: {len(batch):,} upserted, {skipped} skipped")


# ── Phase: event_annotations ──────────────────────────────────────────────────

def load_event_annotations(conn, rows, market_ids):
    print(f"Loading event_annotations ({len(rows):,} rows)...")
    cur = conn.cursor()
    skipped = 0
    unknown_types = set()
    batch = []

    allowed_types = {"fomc", "cpi", "core_cpi", "gdp", "payrolls", "other"}

    for row in rows:
        market_id = market_ids.get(row.get("market_type"))
        if market_id is None:
            skipped += 1
            continue
        type_val = row.get("type")
        if type_val and type_val not in allowed_types:
            unknown_types.add(type_val)
            skipped += 1
            continue
        batch.append((
            market_id,
            row.get("event_date"),
            row.get("label"),
            type_val,
            row.get("notes"),
        ))

    cur.executemany("""
        INSERT INTO event_annotations
            (market_id, event_date, label, type, notes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(market_id, event_date, label) DO UPDATE SET
            type = excluded.type,
            notes = excluded.notes;
    """, batch)
    conn.commit()
    print(f"  done: {len(batch):,} upserted, {skipped} skipped")
    if unknown_types:
        print(f"  WARNING: rejected unknown annotation types: {sorted(unknown_types)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Load Airtable data into v2 SQLite schema.")
    parser.add_argument("--db", default=DEFAULT_DB_PATH,
                        help=f"path to SQLite database (default: {DEFAULT_DB_PATH})")
    parser.add_argument("--truncate", action="store_true",
                        help="delete all existing data rows before loading")
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch from Airtable and report counts, but don't write")
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db)
    if not os.path.exists(db_path):
        print(f"ERROR: database not found at {db_path}")
        print(f"       create it first: sqlite3 {db_path} < schema_v2.sql")
        sys.exit(1)

    started = datetime.now()
    print(f"ETL v2 started at {started.isoformat(timespec='seconds')}")
    print(f"Source:  Airtable base {AIRTABLE_BASE_ID}")
    print(f"Target:  {db_path}")
    if args.dry_run:
        print("Mode:    DRY RUN (no writes)")
    print()

    print("Phase 1: fetch from Airtable")
    api = Api(AIRTABLE_API_KEY)
    contract_details   = fetch_table(api, "Contract_Details")
    dashboard_summary  = fetch_table(api, "Dashboard_Summary")
    accuracy_log       = fetch_table(api, "Accuracy_Log")
    event_annotations  = fetch_table(api, "Event_Annotations")
    print()

    if args.dry_run:
        print("Dry run — exiting before any writes.")
        return

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    # Sanity check: confirm we're pointed at a v2 database
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='contracts';")
    if cur.fetchone() is None:
        print(f"ERROR: {db_path} is missing the `contracts` table.")
        print(f"       This looks like a v1 database. Either:")
        print(f"         - point --db at a v2 database, or")
        print(f"         - rebuild this one: rm {db_path} && sqlite3 {db_path} < schema_v2.sql")
        conn.close()
        sys.exit(1)

    try:
        if args.truncate:
            print("Phase 2: truncate")
            truncate_data(conn)
            print()

        market_ids = load_market_lookup(conn)
        if not market_ids:
            print("ERROR: markets table is empty. Run schema_v2.sql first.")
            sys.exit(1)

        print("Phase 3: contract_series")
        series_ids = derive_and_upsert_contract_series(
            conn, market_ids, contract_details, dashboard_summary, accuracy_log
        )
        print()

        print("Phase 4: contracts")
        cumulative_contracts, bucket_contracts = derive_and_upsert_contracts(
            conn, market_ids, series_ids, contract_details
        )
        print()

        print("Phase 5: data tables")
        load_contract_details(conn, contract_details, series_ids,
                              cumulative_contracts, bucket_contracts)
        load_dashboard_summary(conn, dashboard_summary, series_ids)
        load_accuracy_log(conn, accuracy_log, market_ids, series_ids)
        load_event_annotations(conn, event_annotations, market_ids)
        print()

        print("Phase 6: summary")
        cur = conn.cursor()
        for table in ["markets", "contract_series", "contracts",
                      "contract_details", "dashboard_summary",
                      "accuracy_log", "event_annotations"]:
            cur.execute(f"SELECT COUNT(*) FROM {table};")
            count = cur.fetchone()[0]
            print(f"  {table:22s}  {count:>8,} rows")

        # Synthetic vs real breakdown
        cur.execute("""
            SELECT
                SUM(CASE WHEN is_synthetic = 1 THEN 1 ELSE 0 END) AS synthetic,
                SUM(CASE WHEN is_synthetic = 0 THEN 1 ELSE 0 END) AS real_contracts
            FROM contracts;
        """)
        syn, real = cur.fetchone()
        print(f"    of which: {real} real, {syn} synthetic (F-prefixed)")

    finally:
        conn.close()

    elapsed = (datetime.now() - started).total_seconds()
    print(f"\nDone in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
