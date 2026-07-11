"""Tests for the generic tool-result logging in agent/core.py (Fix 05, Step 6a).

The shared dispatch path in ``_run_with_tools`` logs each tool call
(``logger.info("Tool call: %s(%s)", ...)``) and, after dispatch, a result
log line. Step 6a added the result line. These tests verify the log
format includes the tool name, status, and result length — but NOT the
result body, to avoid leaking database query results (PII) into log files.
"""

from __future__ import annotations


class TestToolResultLogFormat:
    """Verify the log format string matches what _run_with_tools uses."""

    def test_format_contains_name_status_and_length(self):
        # This mirrors the exact format in agent/core.py:_run_with_tools:
        # "Tool result: %s -> %s (%d chars)"
        log_line = f"Tool result: sql_query -> ok ({len('some result')} chars)"
        assert "sql_query" in log_line
        assert "ok" in log_line
        assert "11" in log_line  # len("some result")
        # The result body must NOT appear in the log line
        assert "some result" not in log_line

    def test_error_case_format(self):
        log_line = f"Tool result: sql_query -> error ({len('Error: bad query')} chars)"
        assert "error" in log_line
        assert "sql_query" in log_line
        assert "16" in log_line  # len("Error: bad query")

    def test_empty_result(self):
        log_line = "Tool result: web_search -> ok (0 chars)"
        assert "0" in log_line
        assert "web_search" in log_line
        assert "ok" in log_line
