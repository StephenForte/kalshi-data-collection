"""
Shared pytest fixtures for Kalshi Monitor tests.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta

import pytest


@pytest.fixture
def mock_env_vars(monkeypatch):
    """Set up mock environment variables for testing."""
    monkeypatch.setenv("KALSHI_API_KEY", "test-api-key")
    monkeypatch.setenv("FRED_API_KEY", "test-fred-key")


@pytest.fixture
def temp_db_path(tmp_path):
    """Create a temporary SQLite database path."""
    return str(tmp_path / "test_kalshi.db")


@pytest.fixture
def initialized_db(temp_db_path, monkeypatch):
    """
    Create a temporary SQLite database with the required schema.
    Returns the database path.
    """
    monkeypatch.setenv("KALSHI_DB_PATH", temp_db_path)
    
    conn = sqlite3.connect(temp_db_path)
    conn.executescript("""
        -- Markets reference table
        CREATE TABLE IF NOT EXISTS markets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT,
            description TEXT
        );
        
        -- Contract series (events)
        CREATE TABLE IF NOT EXISTS contract_series (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            market_id INTEGER NOT NULL,
            strike_structure TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        
        -- Individual contracts
        CREATE TABLE IF NOT EXISTS contracts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_series_id INTEGER NOT NULL,
            code TEXT UNIQUE NOT NULL,
            strike REAL,
            label TEXT,
            is_synthetic INTEGER DEFAULT 0,
            FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
        );
        
        -- Contract price/volume snapshots
        CREATE TABLE IF NOT EXISTS contract_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            yes_price REAL,
            implied_probability REAL,
            volume INTEGER,
            days_to_event INTEGER,
            market_volume_usd INTEGER,
            FOREIGN KEY (contract_id) REFERENCES contracts(id)
        );
        
        -- Dashboard summary
        CREATE TABLE IF NOT EXISTS dashboard_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contract_series_id INTEGER NOT NULL,
            timestamp TEXT NOT NULL,
            implied_mean REAL,
            days_to_event INTEGER,
            market_volume_usd INTEGER,
            std_dev REAL,
            skewness REAL,
            UNIQUE(contract_series_id, timestamp),
            FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
        );
        
        -- Accuracy log
        CREATE TABLE IF NOT EXISTS accuracy_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            contract_series_id INTEGER,
            run_date TEXT NOT NULL,
            release_date TEXT NOT NULL,
            kalshi_implied_mean REAL,
            actual_value REAL,
            error REAL,
            abs_error REAL,
            days_before INTEGER,
            readings_in_window INTEGER,
            UNIQUE(market_id, release_date),
            FOREIGN KEY (market_id) REFERENCES markets(id),
            FOREIGN KEY (contract_series_id) REFERENCES contract_series(id)
        );
        
        -- Event annotations
        CREATE TABLE IF NOT EXISTS event_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id INTEGER NOT NULL,
            event_date TEXT NOT NULL,
            label TEXT,
            type TEXT,
            notes TEXT,
            FOREIGN KEY (market_id) REFERENCES markets(id)
        );
        
        -- View for dashboard summary
        CREATE VIEW IF NOT EXISTS v_dashboard_summary AS
        SELECT
            ds.timestamp,
            m.code AS market_type,
            cs.code AS contract_series,
            ds.implied_mean,
            ds.std_dev,
            ds.skewness,
            ds.market_volume_usd,
            ds.days_to_event,
            cs.strike_structure
        FROM dashboard_summary ds
        JOIN contract_series cs ON cs.id = ds.contract_series_id
        JOIN markets m ON m.id = cs.market_id;
        
        -- View for accuracy log
        CREATE VIEW IF NOT EXISTS v_accuracy_log AS
        SELECT
            m.code AS market_type,
            cs.code AS contract_series,
            al.run_date,
            al.release_date,
            al.kalshi_implied_mean,
            al.actual_value,
            al.error,
            al.abs_error,
            al.days_before,
            al.readings_in_window
        FROM accuracy_log al
        JOIN markets m ON m.id = al.market_id
        LEFT JOIN contract_series cs ON cs.id = al.contract_series_id;
        
        -- View for event annotations
        CREATE VIEW IF NOT EXISTS v_event_annotations AS
        SELECT
            m.code AS market_type,
            ea.event_date,
            ea.label,
            ea.type,
            ea.notes
        FROM event_annotations ea
        JOIN markets m ON m.id = ea.market_id;
        
        -- Insert default markets
        INSERT INTO markets (code, name) VALUES 
            ('CPI', 'CPI Year over Year'),
            ('Core_CPI_MoM', 'Core CPI Month over Month'),
            ('GDP', 'GDP Growth'),
            ('Recession', 'Recession Probability'),
            ('FF_Rate', 'Fed Funds Rate'),
            ('Payrolls', 'Payrolls');
    """)
    conn.commit()
    conn.close()
    
    return temp_db_path


@pytest.fixture
def sample_markets_response():
    """Sample response from Kalshi markets API."""
    return {
        "markets": [
            {
                "ticker": "KXCPIYOY-25JAN-T2.5",
                "floor_strike": 2.5,
                "status": "active",
                "last_price": 75,
                "last_price_dollars": None,
                "volume": 1000,
                "volume_fp": "1000.00",
                "expected_expiration_time": "2025-02-15T14:00:00Z",
            },
            {
                "ticker": "KXCPIYOY-25JAN-T2.6",
                "floor_strike": 2.6,
                "status": "active",
                "last_price": 50,
                "last_price_dollars": None,
                "volume": 800,
                "volume_fp": "800.00",
                "expected_expiration_time": "2025-02-15T14:00:00Z",
            },
            {
                "ticker": "KXCPIYOY-25JAN-T2.7",
                "floor_strike": 2.7,
                "status": "active",
                "last_price": 25,
                "last_price_dollars": None,
                "volume": 500,
                "volume_fp": "500.00",
                "expected_expiration_time": "2025-02-15T14:00:00Z",
            },
        ]
    }


@pytest.fixture
def sample_events_response():
    """Sample response from Kalshi events API."""
    return {
        "events": [
            {
                "event_ticker": "KXCPIYOY-25JAN",
                "title": "CPI YoY January 2025",
                "strike_date": "2025-02-15T14:00:00Z",
            },
            {
                "event_ticker": "KXCPIYOY-25FEB",
                "title": "CPI YoY February 2025",
                "strike_date": "2025-03-15T14:00:00Z",
            },
        ]
    }


@pytest.fixture
def sample_event_with_markets_response(sample_markets_response):
    """Sample response for a single event with markets."""
    return {
        "event": {
            "event_ticker": "KXCPIYOY-25JAN",
            "title": "CPI YoY January 2025",
            "strike_date": "2025-02-15T14:00:00Z",
        },
        "markets": sample_markets_response["markets"],
    }


@pytest.fixture
def sample_me_markets_response():
    """Sample mutually exclusive markets response (e.g., Recession)."""
    return {
        "markets": [
            {
                "ticker": "KXNBERRECESSQ-Q1-2025",
                "status": "active",
                "last_price": 15,
                "last_price_dollars": None,
                "volume": 2000,
                "volume_fp": "2000.00",
                "yes_sub_title": "Q1 2025",
            },
            {
                "ticker": "KXNBERRECESSQ-Q2-2025",
                "status": "active",
                "last_price": 20,
                "last_price_dollars": None,
                "volume": 1500,
                "volume_fp": "1500.00",
                "yes_sub_title": "Q2 2025",
            },
            {
                "ticker": "KXNBERRECESSQ-Q3-2025",
                "status": "active",
                "last_price": 10,
                "last_price_dollars": None,
                "volume": 1000,
                "volume_fp": "1000.00",
                "yes_sub_title": "Q3 2025",
            },
        ]
    }


@pytest.fixture
def sample_fred_response():
    """Sample FRED API response."""
    return {
        "observations": [
            {"date": "2024-01-01", "value": "308.417"},
            {"date": "2024-02-01", "value": "309.685"},
            {"date": "2024-03-01", "value": "310.326"},
            {"date": "2024-04-01", "value": "310.856"},
            {"date": "2024-05-01", "value": "311.056"},
            {"date": "2024-06-01", "value": "311.202"},
            {"date": "2024-07-01", "value": "311.414"},
            {"date": "2024-08-01", "value": "311.561"},
            {"date": "2024-09-01", "value": "311.856"},
            {"date": "2024-10-01", "value": "312.103"},
            {"date": "2024-11-01", "value": "312.443"},
            {"date": "2024-12-01", "value": "312.798"},
            {"date": "2025-01-01", "value": "316.615"},
        ]
    }


@pytest.fixture
def sample_snapshot():
    """Sample market snapshot for testing export/dashboard."""
    now = datetime.now(timezone.utc)
    return {
        "generated_at": now.isoformat(),
        "trend_days": 14,
        "markets": {
            "CPI": {
                "label": "CPI (YoY)",
                "contract_series": "KXCPIYOY-25JAN",
                "latest_run": now.isoformat(),
                "implied_mean": 2.65,
                "std_dev": 0.15,
                "skewness": 0.12,
                "market_volume_usd": 50000,
                "days_to_event": 30,
                "is_probability": False,
                "trend": [
                    {"timestamp": (now - timedelta(days=1)).isoformat(), "implied_mean": 2.60, "std_dev": 0.14, "skewness": 0.10},
                    {"timestamp": now.isoformat(), "implied_mean": 2.65, "std_dev": 0.15, "skewness": 0.12},
                ],
                "distribution": [
                    {"label": "2.5%-2.6%", "strike": 2.5, "probability": 0.25, "yes_price": 0.75, "volume": 1000},
                    {"label": "2.6%-2.7%", "strike": 2.6, "probability": 0.25, "yes_price": 0.50, "volume": 800},
                    {"label": "≥ 2.7%", "strike": 2.7, "probability": 0.25, "yes_price": 0.25, "volume": 500},
                ],
                "annotations": [],
            },
        },
        "accuracy": {},
    }


@pytest.fixture
def temp_snapshot_file(tmp_path, sample_snapshot):
    """Create a temporary snapshot.json file."""
    import json
    snapshot_path = tmp_path / "snapshot.json"
    with open(snapshot_path, "w") as f:
        json.dump(sample_snapshot, f)
    return str(snapshot_path)


@pytest.fixture
def temp_dashboard_template(tmp_path):
    """Create a temporary dashboard HTML template."""
    template_path = tmp_path / "kalshi_dashboard.html"
    template_content = """<!DOCTYPE html>
<html>
<head>
    <title>Kalshi Dashboard</title>
</head>
<body>
    <header>
        <span>Generated: 2024-01-01 12:00 UTC</span>
    </header>
    <script>
const data = {"generated_at":"2024-01-01T12:00:00+00:00","markets":{},"accuracy":{}};
    </script>
</body>
</html>
"""
    with open(template_path, "w") as f:
        f.write(template_content)
    return str(template_path)
