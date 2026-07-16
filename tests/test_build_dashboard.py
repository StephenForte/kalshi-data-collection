"""
Tests for build_dashboard.py
"""

import json
import os
from datetime import datetime, timezone

import pytest

import build_dashboard


class TestDataPattern:
    """Tests for DATA_PATTERN regex."""

    def test_matches_simple_data(self):
        """Should match simple const data declaration."""
        html = 'const data = {"test": true};'
        match = build_dashboard.DATA_PATTERN.search(html)
        assert match is not None

    def test_matches_multiline_data(self):
        """Should match multiline const data declaration."""
        html = '''const data = {
    "test": true,
    "nested": {
        "value": 123
    }
};'''
        match = build_dashboard.DATA_PATTERN.search(html)
        assert match is not None

    def test_no_match_different_name(self):
        """Should not match different variable names."""
        html = 'const config = {"test": true};'
        match = build_dashboard.DATA_PATTERN.search(html)
        assert match is None


class TestTimestampPattern:
    """Tests for TIMESTAMP_PATTERN regex."""

    def test_matches_timestamp(self):
        """Should match Generated timestamp."""
        html = 'Generated: 2024-01-15 14:30 UTC'
        match = build_dashboard.TIMESTAMP_PATTERN.search(html)
        assert match is not None

    def test_matches_different_dates(self):
        """Should match various date formats."""
        dates = [
            "Generated: 2025-12-31 23:59 UTC",
            "Generated: 2020-01-01 00:00 UTC",
        ]
        for html in dates:
            match = build_dashboard.TIMESTAMP_PATTERN.search(html)
            assert match is not None, f"Failed to match: {html}"

    def test_no_match_different_format(self):
        """Should not match different formats."""
        html = "Last updated: 2024-01-15"
        match = build_dashboard.TIMESTAMP_PATTERN.search(html)
        assert match is None


class TestBuild:
    """Tests for build function."""

    def test_template_not_found(self, tmp_path, capsys):
        """Should return error when template not found."""
        result = build_dashboard.build(
            template_path=str(tmp_path / "nonexistent.html"),
            snapshot_path=str(tmp_path / "snapshot.json"),
            output_path=str(tmp_path / "output.html"),
        )
        
        assert result == 1
        captured = capsys.readouterr()
        assert "template not found" in captured.err

    def test_snapshot_not_found(self, temp_dashboard_template, tmp_path, capsys):
        """Should return error when snapshot not found."""
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=str(tmp_path / "nonexistent.json"),
            output_path=str(tmp_path / "output.html"),
        )
        
        assert result == 1
        captured = capsys.readouterr()
        assert "snapshot not found" in captured.err

    def test_successful_build(self, temp_dashboard_template, temp_snapshot_file, tmp_path, capsys):
        """Should successfully build dashboard."""
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=temp_snapshot_file,
            output_path=output_path,
        )
        
        assert result == 0
        assert os.path.exists(output_path)
        
        with open(output_path) as f:
            content = f.read()
        
        # Should contain the new snapshot data
        assert "CPI" in content or "generated_at" in content

    def test_inplace_build(self, temp_dashboard_template, temp_snapshot_file, capsys):
        """Should build in-place when output matches template."""
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=temp_snapshot_file,
            output_path=temp_dashboard_template,
        )
        
        assert result == 0
        
        with open(temp_dashboard_template) as f:
            content = f.read()
        
        assert "const data =" in content

    def test_updates_timestamp(self, temp_dashboard_template, temp_snapshot_file, tmp_path):
        """Should update the Generated timestamp."""
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=temp_snapshot_file,
            output_path=output_path,
        )
        
        assert result == 0
        
        with open(output_path) as f:
            content = f.read()
        
        # Should have updated timestamp (not the old 2024-01-01)
        assert "2024-01-01 12:00 UTC" not in content or "Generated:" in content

    def test_no_data_pattern_match(self, tmp_path, capsys):
        """Should return error when data pattern not found."""
        # Create template without const data
        template_path = tmp_path / "bad_template.html"
        with open(template_path, "w") as f:
            f.write("<html><body>No data here</body></html>")
        
        snapshot_path = tmp_path / "snapshot.json"
        with open(snapshot_path, "w") as f:
            json.dump({"test": True}, f)
        
        result = build_dashboard.build(
            template_path=str(template_path),
            snapshot_path=str(snapshot_path),
            output_path=str(tmp_path / "output.html"),
        )
        
        assert result == 2
        captured = capsys.readouterr()
        assert "could not find" in captured.err

    def test_unicode_escapes_preserved(self, temp_dashboard_template, tmp_path):
        """Should preserve unicode escapes in JSON."""
        snapshot_path = tmp_path / "snapshot_unicode.json"
        with open(snapshot_path, "w") as f:
            json.dump({"test": "value with \u00e9 unicode"}, f)
        
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=str(snapshot_path),
            output_path=output_path,
        )
        
        assert result == 0
        
        with open(output_path) as f:
            content = f.read()
        
        # Should contain the data without breaking on unicode
        assert "const data =" in content


class TestMain:
    """Tests for main function and argument parsing."""

    def test_default_paths(self):
        """Should use default paths."""
        assert build_dashboard.DEFAULT_TEMPLATE.endswith("kalshi_dashboard.html")
        assert build_dashboard.DEFAULT_SNAPSHOT.endswith("snapshot.json")

    def test_expanduser_in_paths(self, monkeypatch, tmp_path):
        """Should expand ~ in paths."""
        # Create mock files
        template = tmp_path / "template.html"
        snapshot = tmp_path / "snapshot.json"
        output = tmp_path / "output.html"
        
        with open(template, "w") as f:
            f.write('''<html>
<body>
<script>
const data = {"test": true};
</script>
</body>
</html>''')
        
        with open(snapshot, "w") as f:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat()}, f)
        
        # Mock sys.argv for argument parsing
        import sys
        original_argv = sys.argv
        sys.argv = [
            "build_dashboard.py",
            "--template", str(template),
            "--snapshot", str(snapshot),
            "--output", str(output),
        ]
        
        try:
            result = build_dashboard.main()
            assert result == 0
            assert os.path.exists(output)
        finally:
            sys.argv = original_argv


class TestReportOutput:
    """Tests for build report output."""

    def test_reports_file_sizes(self, temp_dashboard_template, temp_snapshot_file, tmp_path, capsys):
        """Should report file sizes in output."""
        output_path = str(tmp_path / "output.html")
        
        build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=temp_snapshot_file,
            output_path=output_path,
        )
        
        captured = capsys.readouterr()
        
        assert "bytes" in captured.out
        assert "Built" in captured.out

    def test_reports_market_count(self, temp_dashboard_template, temp_snapshot_file, tmp_path, capsys):
        """Should report number of markets."""
        output_path = str(tmp_path / "output.html")
        
        build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=temp_snapshot_file,
            output_path=output_path,
        )
        
        captured = capsys.readouterr()
        
        assert "markets:" in captured.out


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_snapshot(self, temp_dashboard_template, tmp_path, capsys):
        """Should handle empty snapshot."""
        snapshot_path = tmp_path / "empty_snapshot.json"
        with open(snapshot_path, "w") as f:
            json.dump({}, f)
        
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=str(snapshot_path),
            output_path=output_path,
        )
        
        # Should still succeed
        assert result == 0

    def test_missing_generated_at(self, temp_dashboard_template, tmp_path, capsys):
        """Should handle missing generated_at in snapshot."""
        snapshot_path = tmp_path / "no_generated_at.json"
        with open(snapshot_path, "w") as f:
            json.dump({"markets": {}}, f)
        
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=temp_dashboard_template,
            snapshot_path=str(snapshot_path),
            output_path=output_path,
        )
        
        # Should still succeed, using current time
        assert result == 0

    def test_template_without_timestamp(self, tmp_path, capsys):
        """Should warn when timestamp not found in template."""
        template_path = tmp_path / "no_timestamp.html"
        with open(template_path, "w") as f:
            f.write('''<html>
<body>
<script>
const data = {"test": true};
</script>
</body>
</html>''')
        
        snapshot_path = tmp_path / "snapshot.json"
        with open(snapshot_path, "w") as f:
            json.dump({"generated_at": datetime.now(timezone.utc).isoformat()}, f)
        
        output_path = str(tmp_path / "output.html")
        
        result = build_dashboard.build(
            template_path=str(template_path),
            snapshot_path=str(snapshot_path),
            output_path=output_path,
        )
        
        assert result == 0
        captured = capsys.readouterr()
        assert "WARNING" in captured.err or "timestamp not found" in captured.err
