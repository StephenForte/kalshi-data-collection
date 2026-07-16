"""
Tests for kalshi_accuracy.py
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest
import responses

import kalshi_accuracy


class TestFetchFredSeries:
    """Tests for fetch_fred_series function."""

    @responses.activate
    def test_valid_response(self, mock_env_vars, sample_fred_response):
        """Should fetch and parse FRED observations."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json=sample_fred_response,
            status=200,
        )
        
        result = kalshi_accuracy.fetch_fred_series("CPIAUCSL")
        
        assert len(result) == 13
        assert result[0] == ("2024-01-01", 308.417)
        assert result[-1] == ("2025-01-01", 316.615)

    @responses.activate
    def test_missing_values_filtered(self, mock_env_vars):
        """Should filter out missing values (marked as '.')."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={
                "observations": [
                    {"date": "2024-01-01", "value": "100.0"},
                    {"date": "2024-02-01", "value": "."},
                    {"date": "2024-03-01", "value": "102.0"},
                ]
            },
            status=200,
        )
        
        result = kalshi_accuracy.fetch_fred_series("TEST")
        
        assert len(result) == 2
        assert result[0][0] == "2024-01-01"
        assert result[1][0] == "2024-03-01"

    @responses.activate
    def test_invalid_values_filtered(self, mock_env_vars):
        """Should filter out invalid numeric values."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={
                "observations": [
                    {"date": "2024-01-01", "value": "100.0"},
                    {"date": "2024-02-01", "value": "invalid"},
                    {"date": "2024-03-01", "value": "102.0"},
                ]
            },
            status=200,
        )
        
        result = kalshi_accuracy.fetch_fred_series("TEST")
        
        assert len(result) == 2

    @responses.activate
    def test_empty_response(self, mock_env_vars):
        """Should return empty list for empty response."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={"observations": []},
            status=200,
        )
        
        result = kalshi_accuracy.fetch_fred_series("TEST")
        
        assert result == []


class TestGetFredValue:
    """Tests for get_fred_value function."""

    @responses.activate
    def test_level_transform(self, mock_env_vars):
        """Level transform should return raw value."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={
                "observations": [
                    {"date": "2024-12-01", "value": "2.5"},
                    {"date": "2025-01-01", "value": "2.8"},
                ]
            },
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("TEST", "2025-01-15", "level")
        
        assert result == 2.8

    @responses.activate
    def test_yoy_transform(self, mock_env_vars, sample_fred_response):
        """YoY transform should calculate year-over-year percentage change."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json=sample_fred_response,
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("CPIAUCSL", "2025-01-15", "yoy")
        
        # (316.615 - 308.417) / 308.417 * 100 ≈ 2.66%
        assert result is not None
        assert 2.0 < result < 3.0

    @responses.activate
    def test_mom_transform(self, mock_env_vars, sample_fred_response):
        """MoM transform should calculate month-over-month percentage change."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json=sample_fred_response,
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("CPILFESL", "2025-01-15", "mom")
        
        # (316.615 - 312.798) / 312.798 * 100 ≈ 1.22%
        assert result is not None

    @responses.activate
    def test_mom_jobs_transform(self, mock_env_vars):
        """MoM jobs transform should calculate change in thousands * 1000."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={
                "observations": [
                    {"date": "2024-12-01", "value": "158000"},
                    {"date": "2025-01-01", "value": "158150"},
                ]
            },
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("PAYEMS", "2025-01-15", "mom_jobs")
        
        # (158150 - 158000) * 1000 = 150000 jobs
        assert result == 150000

    @responses.activate
    def test_no_observations(self, mock_env_vars):
        """Should return None when no observations."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={"observations": []},
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("TEST", "2025-01-15", "level")
        
        assert result is None

    @responses.activate
    def test_insufficient_history_for_yoy(self, mock_env_vars):
        """Should return None when insufficient history for YoY."""
        responses.add(
            responses.GET,
            "https://api.stlouisfed.org/fred/series/observations",
            json={
                "observations": [
                    {"date": "2024-06-01", "value": "100.0"},
                    {"date": "2024-07-01", "value": "101.0"},
                ]
            },
            status=200,
        )
        
        result = kalshi_accuracy.get_fred_value("TEST", "2024-07-15", "yoy")
        
        assert result is None


class TestInferReleaseDate:
    """Tests for infer_release_date function."""

    def test_days_to_event_zero(self):
        """Should use date when days_to_event first hit 0."""
        records = [
            {"timestamp": "2025-01-10T12:00:00Z", "days_to_event": 5},
            {"timestamp": "2025-01-15T12:00:00Z", "days_to_event": 0},
            {"timestamp": "2025-01-16T12:00:00Z", "days_to_event": 0},
        ]
        
        result = kalshi_accuracy.infer_release_date("CPI", "TEST", records)
        
        assert result == "2025-01-15"

    def test_min_days_fallback(self):
        """Should use date of minimum days_to_event."""
        records = [
            {"timestamp": "2025-01-10T12:00:00Z", "days_to_event": 5},
            {"timestamp": "2025-01-12T12:00:00Z", "days_to_event": 3},
            {"timestamp": "2025-01-13T12:00:00Z", "days_to_event": 2},
        ]
        
        result = kalshi_accuracy.infer_release_date("CPI", "TEST", records)
        
        assert result == "2025-01-13"

    def test_no_timestamp(self):
        """Should return None when no valid timestamps."""
        records = [{"days_to_event": 5}]
        
        result = kalshi_accuracy.infer_release_date("CPI", "TEST", records)
        
        assert result is None


class TestGetPreReleaseAverage:
    """Tests for get_pre_release_average function."""

    def test_valid_window(self):
        """Should average implied_mean in the window."""
        release_date = "2025-01-15"
        # Window is 72 hours before 2pm UTC on release_date
        # 2025-01-15 14:00 UTC - 72h = 2025-01-12 14:00 UTC
        records = [
            {"timestamp": "2025-01-12T15:00:00Z", "implied_mean": 2.50},  # Inside window
            {"timestamp": "2025-01-13T12:00:00Z", "implied_mean": 2.55},  # Inside window
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},  # Inside window
        ]
        
        result = kalshi_accuracy.get_pre_release_average(records, release_date)
        
        # Average of 2.50, 2.55, 2.60 = 2.55
        assert result == 2.55

    def test_outside_window(self):
        """Should exclude records outside the window."""
        release_date = "2025-01-15"
        records = [
            {"timestamp": "2025-01-01T12:00:00Z", "implied_mean": 3.00},  # Too early
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},
        ]
        
        result = kalshi_accuracy.get_pre_release_average(records, release_date)
        
        assert result == 2.60

    def test_no_records_in_window(self):
        """Should return None when no records in window."""
        release_date = "2025-01-15"
        records = [
            {"timestamp": "2025-01-01T12:00:00Z", "implied_mean": 2.50},  # Too early
        ]
        
        result = kalshi_accuracy.get_pre_release_average(records, release_date)
        
        assert result is None

    def test_none_implied_mean_filtered(self):
        """Should filter out records with None implied_mean."""
        release_date = "2025-01-15"
        records = [
            {"timestamp": "2025-01-13T12:00:00Z", "implied_mean": None},
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},
        ]
        
        result = kalshi_accuracy.get_pre_release_average(records, release_date)
        
        assert result == 2.60


class TestCountReadingsInWindow:
    """Tests for count_readings_in_window function."""

    def test_count_in_window(self):
        """Should count records in the pre-release window."""
        release_date = "2025-01-15"
        # Window is 72 hours before 2pm UTC on release_date
        # 2025-01-15 14:00 UTC - 72h = 2025-01-12 14:00 UTC
        records = [
            {"timestamp": "2025-01-12T15:00:00Z", "implied_mean": 2.50},  # Inside window
            {"timestamp": "2025-01-13T12:00:00Z", "implied_mean": 2.55},  # Inside window
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},  # Inside window
        ]
        
        count = kalshi_accuracy.count_readings_in_window(records, release_date)
        
        assert count == 3

    def test_excludes_outside_window(self):
        """Should exclude records outside window."""
        release_date = "2025-01-15"
        records = [
            {"timestamp": "2025-01-01T12:00:00Z", "implied_mean": 2.40},  # Too early
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},
        ]
        
        count = kalshi_accuracy.count_readings_in_window(records, release_date)
        
        assert count == 1

    def test_excludes_none_implied_mean(self):
        """Should exclude records with None implied_mean."""
        release_date = "2025-01-15"
        records = [
            {"timestamp": "2025-01-13T12:00:00Z", "implied_mean": None},
            {"timestamp": "2025-01-14T12:00:00Z", "implied_mean": 2.60},
        ]
        
        count = kalshi_accuracy.count_readings_in_window(records, release_date)
        
        assert count == 1


class TestDatabaseFunctions:
    """Tests for database operations."""

    def test_load_market_lookup(self, initialized_db, monkeypatch):
        """Should load market codes and IDs."""
        original_db_path = kalshi_accuracy.DB_PATH
        kalshi_accuracy.DB_PATH = initialized_db
        
        try:
            conn = kalshi_accuracy.open_db()
            market_ids = kalshi_accuracy.load_market_lookup(conn)
            conn.close()
            
            assert "CPI" in market_ids
            assert "GDP" in market_ids
        finally:
            kalshi_accuracy.DB_PATH = original_db_path

    def test_load_series_lookup(self, initialized_db, monkeypatch):
        """Should load series codes and IDs."""
        original_db_path = kalshi_accuracy.DB_PATH
        kalshi_accuracy.DB_PATH = initialized_db
        
        try:
            # Insert a series
            conn = sqlite3.connect(initialized_db)
            conn.execute("""
                INSERT INTO contract_series (code, market_id, strike_structure)
                VALUES ('KXCPIYOY-25JAN', 1, 'cumulative')
            """)
            conn.commit()
            conn.close()
            
            conn = kalshi_accuracy.open_db()
            series_ids = kalshi_accuracy.load_series_lookup(conn)
            conn.close()
            
            assert "KXCPIYOY-25JAN" in series_ids
        finally:
            kalshi_accuracy.DB_PATH = original_db_path

    def test_get_already_logged(self, initialized_db, monkeypatch):
        """Should return set of already logged series."""
        original_db_path = kalshi_accuracy.DB_PATH
        kalshi_accuracy.DB_PATH = initialized_db
        
        try:
            # Insert a series and accuracy log entry
            conn = sqlite3.connect(initialized_db)
            conn.execute("""
                INSERT INTO contract_series (code, market_id, strike_structure)
                VALUES ('KXCPIYOY-25JAN', 1, 'cumulative')
            """)
            conn.execute("""
                INSERT INTO accuracy_log 
                (market_id, contract_series_id, run_date, release_date, kalshi_implied_mean, actual_value, error, abs_error)
                VALUES (1, 1, '2025-01-20', '2025-01-15', 2.65, 2.60, 0.05, 0.05)
            """)
            conn.commit()
            conn.close()
            
            conn = kalshi_accuracy.open_db()
            logged = kalshi_accuracy.get_already_logged(conn)
            conn.close()
            
            assert "KXCPIYOY-25JAN" in logged
        finally:
            kalshi_accuracy.DB_PATH = original_db_path

    def test_insert_accuracy_row(self, initialized_db, monkeypatch):
        """Should insert accuracy row."""
        original_db_path = kalshi_accuracy.DB_PATH
        kalshi_accuracy.DB_PATH = initialized_db
        
        try:
            # Insert a series first
            conn = sqlite3.connect(initialized_db)
            conn.execute("""
                INSERT INTO contract_series (code, market_id, strike_structure)
                VALUES ('KXCPIYOY-25JAN', 1, 'cumulative')
            """)
            conn.commit()
            conn.close()
            
            conn = kalshi_accuracy.open_db()
            kalshi_accuracy.insert_accuracy_row(
                conn,
                market_id=1,
                series_id=1,
                run_date="2025-01-20",
                release_date="2025-01-15",
                kalshi_mean=2.65,
                actual_value=2.60,
                error=0.05,
                abs_error=0.05,
                days_before=3,
                readings_in_window=10,
            )
            conn.commit()
            
            # Verify insertion
            cur = conn.cursor()
            cur.execute("SELECT * FROM accuracy_log WHERE market_id = 1")
            row = cur.fetchone()
            assert row is not None
            
            conn.close()
        finally:
            kalshi_accuracy.DB_PATH = original_db_path


class TestMarketConfig:
    """Tests for market configuration."""

    def test_all_markets_have_config(self):
        """All markets should have required config."""
        required_keys = {"fred_series", "transform", "units"}
        
        for market, config in kalshi_accuracy.MARKET_CONFIG.items():
            assert required_keys <= set(config.keys()), f"{market} missing keys"

    def test_valid_transforms(self):
        """All transforms should be valid."""
        valid_transforms = {"level", "yoy", "mom", "mom_jobs"}
        
        for market, config in kalshi_accuracy.MARKET_CONFIG.items():
            assert config["transform"] in valid_transforms, f"{market} has invalid transform"
