# kalshi-data-collection

Collects and exports macro market data from Kalshi, then writes both detailed and summary records to Airtable.

## What this project does

- Pulls active Kalshi macro events (CPI, Core CPI, GDP, Recession, Fed Funds, Payrolls)
- Computes implied distributions / summary metrics
- Writes output to Airtable tables:
  - `Contract_Details`
  - `Dashboard_Summary`
- Generates `data/snapshot.json` for downstream consumers

## Requirements

- Python 3.9+
- Airtable API credentials
- Kalshi API key

Install dependencies:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Environment variables

Set these before running:

- `KALSHI_API_KEY`
- `AIRTABLE_API_KEY`
- `AIRTABLE_BASE_ID`

Example:

```bash
export KALSHI_API_KEY="your_kalshi_key"
export AIRTABLE_API_KEY="your_airtable_key"
export AIRTABLE_BASE_ID="your_airtable_base_id"
```

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
