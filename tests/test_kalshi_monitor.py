"""
Tests for kalshi_monitor.py
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest
import responses

import kalshi_monitor


class TestMarketYesPrice:
    """Tests for _market_yes_price function."""

    def test_last_price_in_cents(self):
        """Last price as integer (cents) should be converted to decimal."""
        market = {"last_price": 75}
        assert kalshi_monitor._market_yes_price(market) == 0.75

    def test_last_price_dollars_string(self):
        """Last price as string dollar amount should be parsed."""
        market = {"last_price_dollars": "0.65"}
        assert kalshi_monitor._market_yes_price(market) == 0.65

    def test_last_price_dollars_invalid_string(self):
        """Invalid string should return 0.0."""
        market = {"last_price_dollars": "invalid"}
        assert kalshi_monitor._market_yes_price(market) == 0.0

    def test_no_price(self):
        """Missing price should return 0.0."""
        market = {}
        assert kalshi_monitor._market_yes_price(market) == 0.0

    def test_none_last_price(self):
        """None last_price should return 0.0."""
        market = {"last_price": None}
        assert kalshi_monitor._market_yes_price(market) == 0.0


class TestParseCumulativeDistribution:
    """Tests for parse_cumulative_distribution function."""

    def test_valid_markets(self, sample_markets_response):
        """Should correctly parse cumulative markets."""
        markets = sample_markets_response["markets"]
        bin_rows, implied_mean, std_dev, skewness = kalshi_monitor.parse_cumulative_distribution(markets)
        
        assert len(bin_rows) == 3
        assert all("ticker" in row for row in bin_rows)
        assert all("strike" in row for row in bin_rows)
        assert all("yes_price" in row for row in bin_rows)
        assert all("bin_prob" in row for row in bin_rows)
        assert implied_mean is not None
        assert std_dev is not None
        assert skewness is not None

    def test_empty_markets(self):
        """Empty markets list should return empty result."""
        bin_rows, implied_mean, std_dev, skewness = kalshi_monitor.parse_cumulative_distribution([])
        assert bin_rows == []
        assert implied_mean is None
        assert std_dev is None
        assert skewness is None

    def test_all_zero_prices(self):
        """Markets with all zero prices should be skipped."""
        markets = [
            {"ticker": "TEST-1", "floor_strike": 2.5, "status": "active", "last_price": 0},
            {"ticker": "TEST-2", "floor_strike": 2.6, "status": "active", "last_price": 0},
        ]
        bin_rows, implied_mean, std_dev, skewness = kalshi_monitor.parse_cumulative_distribution(markets)
        assert bin_rows == []

    def test_inactive_markets_filtered(self):
        """Inactive markets should be filtered out."""
        markets = [
            {"ticker": "TEST-1", "floor_strike": 2.5, "status": "active", "last_price": 75},
            {"ticker": "TEST-2", "floor_strike": 2.6, "status": "closed", "last_price": 50},
        ]
        bin_rows, implied_mean, std_dev, skewness = kalshi_monitor.parse_cumulative_distribution(markets)
        assert len(bin_rows) == 1
        assert bin_rows[0]["strike"] == 2.5


class TestParseMutuallyExclusive:
    """Tests for parse_mutually_exclusive function."""

    def test_valid_markets(self, sample_me_markets_response):
        """Should correctly parse mutually exclusive markets."""
        markets = sample_me_markets_response["markets"]
        rows, modal_prob = kalshi_monitor.parse_mutually_exclusive(markets)
        
        assert len(rows) == 3
        assert all("ticker" in row for row in rows)
        assert all("label" in row for row in rows)
        assert all("yes_price" in row for row in rows)
        assert all("prob" in row for row in rows)
        assert modal_prob == 0.20  # Highest probability is 20%

    def test_empty_markets(self):
        """Empty markets list should return empty result."""
        rows, modal_prob = kalshi_monitor.parse_mutually_exclusive([])
        assert rows == []
        assert modal_prob == 0.0

    def test_inactive_markets_filtered(self):
        """Inactive markets should be filtered out."""
        markets = [
            {"ticker": "TEST-1", "status": "active", "last_price": 25, "yes_sub_title": "Option 1"},
            {"ticker": "TEST-2", "status": "closed", "last_price": 75, "yes_sub_title": "Option 2"},
        ]
        rows, modal_prob = kalshi_monitor.parse_mutually_exclusive(markets)
        assert len(rows) == 1


class TestDaysToEvent:
    """Tests for days_to_event function."""

    def test_strike_date_future(self):
        """Future strike date should return positive days."""
        future = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        event = {"strike_date": future}
        days = kalshi_monitor.days_to_event(event)
        assert days == 30 or days == 29  # Allow for timezone edge cases

    def test_strike_date_past(self):
        """Past strike date should return 0."""
        past = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
        event = {"strike_date": past}
        days = kalshi_monitor.days_to_event(event)
        assert days == 0

    def test_strike_date_today(self):
        """Today's strike date should return 0."""
        today = datetime.now(timezone.utc).isoformat()
        event = {"strike_date": today}
        days = kalshi_monitor.days_to_event(event)
        assert days == 0

    def test_invalid_strike_date(self):
        """Invalid strike date should fall back to markets."""
        future = (datetime.now(timezone.utc) + timedelta(days=15)).isoformat()
        event = {"strike_date": "0001-01-01T00:00:00Z"}
        markets = [{"expected_expiration_time": future}]
        days = kalshi_monitor.days_to_event(event, markets)
        assert days == 15 or days == 14

    def test_no_date_info(self):
        """No date info should return None."""
        event = {}
        days = kalshi_monitor.days_to_event(event)
        assert days is None


class TestGetMarketVolumeUsd:
    """Tests for get_market_volume_usd function."""

    def test_valid_volumes(self):
        """Should sum volume_fp across markets."""
        markets = [
            {"volume_fp": "1000.50"},
            {"volume_fp": "500.25"},
            {"volume_fp": "250.00"},
        ]
        total = kalshi_monitor.get_market_volume_usd(markets)
        assert total == 1750

    def test_missing_volume(self):
        """Missing volume_fp should be treated as 0."""
        markets = [
            {"volume_fp": "1000.00"},
            {},
            {"volume_fp": None},
        ]
        total = kalshi_monitor.get_market_volume_usd(markets)
        assert total == 1000

    def test_empty_markets(self):
        """Empty markets list should return None."""
        total = kalshi_monitor.get_market_volume_usd([])
        assert total is None


class TestDatabaseOperations:
    """Tests for database operations."""

    def test_open_db_not_found(self, monkeypatch, tmp_path):
        """Should raise FileNotFoundError when DB doesn't exist."""
        monkeypatch.setenv("KALSHI_DB_PATH", str(tmp_path / "nonexistent.db"))
        
        # Temporarily update the module's DB_PATH
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = str(tmp_path / "nonexistent.db")
        
        try:
            with pytest.raises(FileNotFoundError):
                kalshi_monitor.open_db()
        finally:
            kalshi_monitor.DB_PATH = original_db_path

    def test_load_market_lookup(self, initialized_db, monkeypatch):
        """Should load market codes and IDs."""
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = initialized_db
        
        try:
            conn = kalshi_monitor.open_db()
            market_ids = kalshi_monitor.load_market_lookup(conn)
            conn.close()
            
            assert "CPI" in market_ids
            assert "GDP" in market_ids
            assert "FF_Rate" in market_ids
            assert len(market_ids) == 6
        finally:
            kalshi_monitor.DB_PATH = original_db_path

    def test_get_or_create_contract_series(self, initialized_db, monkeypatch):
        """Should create and retrieve contract series."""
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = initialized_db
        
        try:
            conn = kalshi_monitor.open_db()
            market_ids = kalshi_monitor.load_market_lookup(conn)
            
            series_id = kalshi_monitor.get_or_create_contract_series(
                conn, "KXCPIYOY-25JAN", market_ids["CPI"], "cumulative"
            )
            assert series_id is not None
            
            # Creating same series should return same ID
            series_id2 = kalshi_monitor.get_or_create_contract_series(
                conn, "KXCPIYOY-25JAN", market_ids["CPI"], "cumulative"
            )
            assert series_id == series_id2
            
            conn.close()
        finally:
            kalshi_monitor.DB_PATH = original_db_path

    def test_get_or_create_contract(self, initialized_db, monkeypatch):
        """Should create and retrieve contracts."""
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = initialized_db
        
        try:
            conn = kalshi_monitor.open_db()
            market_ids = kalshi_monitor.load_market_lookup(conn)
            
            series_id = kalshi_monitor.get_or_create_contract_series(
                conn, "KXCPIYOY-25JAN", market_ids["CPI"], "cumulative"
            )
            
            contract_id = kalshi_monitor.get_or_create_contract(
                conn, series_id, "KXCPIYOY-25JAN-T2.5", 2.5, None
            )
            assert contract_id is not None
            
            # Creating same contract should return same ID
            contract_id2 = kalshi_monitor.get_or_create_contract(
                conn, series_id, "KXCPIYOY-25JAN-T2.5", 2.5, None
            )
            assert contract_id == contract_id2
            
            conn.close()
        finally:
            kalshi_monitor.DB_PATH = original_db_path

    def test_write_contract_detail(self, initialized_db, monkeypatch):
        """Should write contract detail snapshots."""
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = initialized_db
        
        try:
            conn = kalshi_monitor.open_db()
            market_ids = kalshi_monitor.load_market_lookup(conn)
            
            series_id = kalshi_monitor.get_or_create_contract_series(
                conn, "KXCPIYOY-25JAN", market_ids["CPI"], "cumulative"
            )
            contract_id = kalshi_monitor.get_or_create_contract(
                conn, series_id, "KXCPIYOY-25JAN-T2.5", 2.5, None
            )
            
            timestamp = datetime.now(timezone.utc).isoformat()
            kalshi_monitor.write_contract_detail(
                conn, contract_id, timestamp, 0.75, 0.25, 1000, 30, 50000
            )
            conn.commit()
            
            # Verify the write
            cur = conn.cursor()
            cur.execute("SELECT * FROM contract_details WHERE contract_id = ?", (contract_id,))
            row = cur.fetchone()
            assert row is not None
            
            conn.close()
        finally:
            kalshi_monitor.DB_PATH = original_db_path

    def test_write_dashboard_summary(self, initialized_db, monkeypatch):
        """Should write dashboard summary rows."""
        original_db_path = kalshi_monitor.DB_PATH
        kalshi_monitor.DB_PATH = initialized_db
        
        try:
            conn = kalshi_monitor.open_db()
            market_ids = kalshi_monitor.load_market_lookup(conn)
            
            series_id = kalshi_monitor.get_or_create_contract_series(
                conn, "KXCPIYOY-25JAN", market_ids["CPI"], "cumulative"
            )
            
            timestamp = datetime.now(timezone.utc).isoformat()
            kalshi_monitor.write_dashboard_summary(
                conn, series_id, timestamp, 2.65, 30, 50000, 0.15, 0.12
            )
            conn.commit()
            
            # Verify the write
            cur = conn.cursor()
            cur.execute("SELECT * FROM dashboard_summary WHERE contract_series_id = ?", (series_id,))
            row = cur.fetchone()
            assert row is not None
            
            conn.close()
        finally:
            kalshi_monitor.DB_PATH = original_db_path


class TestKalshiHeaders:
    """Tests for kalshi_headers function."""

    def test_headers_format(self, mock_env_vars):
        """Should return properly formatted headers."""
        headers = kalshi_monitor.kalshi_headers()
        assert "Authorization" in headers
        assert headers["Authorization"] == "Bearer test-api-key"
        assert headers["Content-Type"] == "application/json"


class TestApiCalls:
    """Tests for Kalshi API calls using responses mock."""

    @responses.activate
    def test_get_event_with_markets(self, mock_env_vars, sample_event_with_markets_response):
        """Should fetch event with markets."""
        event_ticker = "KXCPIYOY-25JAN"
        responses.add(
            responses.GET,
            f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}",
            json=sample_event_with_markets_response,
            status=200,
        )
        
        result = kalshi_monitor.get_event_with_markets(event_ticker)
        
        assert "event" in result
        assert "markets" in result
        assert result["event"]["event_ticker"] == event_ticker

    @responses.activate
    def test_get_active_events(self, mock_env_vars, sample_events_response, sample_event_with_markets_response):
        """Should fetch and filter active events."""
        series_ticker = "KXCPIYOY"
        
        # Mock the series events list
        responses.add(
            responses.GET,
            "https://api.elections.kalshi.com/trade-api/v2/events",
            json=sample_events_response,
            status=200,
        )
        
        # Mock individual event fetches
        for event in sample_events_response["events"]:
            event_ticker = event["event_ticker"]
            responses.add(
                responses.GET,
                f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}",
                json={
                    "event": event,
                    "markets": sample_event_with_markets_response["markets"],
                },
                status=200,
            )
        
        events = kalshi_monitor.get_active_events(series_ticker, limit=2)
        
        assert len(events) <= 2
        assert all("event_ticker" in e for e in events)

    @responses.activate
    def test_get_active_event(self, mock_env_vars, sample_events_response, sample_event_with_markets_response):
        """Should fetch single active event."""
        series_ticker = "KXCPIYOY"
        
        responses.add(
            responses.GET,
            "https://api.elections.kalshi.com/trade-api/v2/events",
            json=sample_events_response,
            status=200,
        )
        
        for event in sample_events_response["events"]:
            event_ticker = event["event_ticker"]
            responses.add(
                responses.GET,
                f"https://api.elections.kalshi.com/trade-api/v2/events/{event_ticker}",
                json={
                    "event": event,
                    "markets": sample_event_with_markets_response["markets"],
                },
                status=200,
            )
        
        event = kalshi_monitor.get_active_event(series_ticker)
        
        assert event is not None
        assert "event_ticker" in event


class TestLogError:
    """Tests for log_error function."""

    def test_log_error_basic(self, capsys):
        """Should print formatted error message."""
        kalshi_monitor.log_error("CPI", "Test error message")
        
        captured = capsys.readouterr()
        assert "[ERROR]" in captured.out
        assert "CPI" in captured.out
        assert "Test error message" in captured.out

    def test_log_error_with_exception(self, capsys):
        """Should print traceback when exception provided."""
        try:
            raise ValueError("Test exception")
        except ValueError as e:
            kalshi_monitor.log_error("GDP", "Exception occurred", exc=e)
        
        captured = capsys.readouterr()
        assert "[ERROR]" in captured.out
        assert "GDP" in captured.out
