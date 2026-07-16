# kalshi-data-collection

Collects and exports macro market data from Kalshi, then writes both detailed and summary records to a local SQLite database.

## What this project does

- Pulls active Kalshi macro events (CPI, Core CPI, GDP, Recession, Fed Funds, Payrolls)
- Computes implied distributions / summary metrics
- Writes output to SQLite tables:
  - `contract_series`
  - `contracts`
  - `contract_details`
  - `dashboard_summary`
- Generates `data/snapshot.json` for downstream consumers
- Builds a local HTML dashboard from `templates/kalshi_dashboard.html` → `data/kalshi_dashboard.html`

## Dashboard

The tracked UX template lives at `templates/kalshi_dashboard.html`. After a snapshot exists:

```bash
python3 build_dashboard.py
```

That inlines `data/snapshot.json` into `data/kalshi_dashboard.html` for offline viewing. The same template can also load `./data/snapshot.json` via fetch (GitHub Pages style).

## Requirements

- Python 3.9+ (includes the standard-library `sqlite3` module)
- A local SQLite database file (default path below)
- Kalshi API key

Install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Set these before running:

- `KALSHI_API_KEY` (required)
- `KALSHI_DB_PATH` (optional; defaults to `~/kalshi-monitor/db/kalshi_macro_data_v2.db`)

Example:

```bash
export KALSHI_API_KEY="your_kalshi_key"
export KALSHI_DB_PATH="~/kalshi-monitor/db/kalshi_macro_data_v2.db"
```

The database file must already exist at `KALSHI_DB_PATH` before running the monitor or export scripts.

## Run

Main monitor run:

```bash
python3 kalshi_monitor.py
```

Export-only / report:

```bash
python3 kalshi_export.py
python3 kalshi_export.py --json
```

Quick local test script:

```bash
python3 test_export.py
```

## Scheduling

`kalshi_run.sh` activates the venv and appends monitor output to `logs/kalshi_monitor.log`. You can call it from cron.
