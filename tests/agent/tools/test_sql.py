"""Tests for the SQL query + describe_database tools (Fix 05)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kai.agent.tools.sql import (
    build_sql_workflow_instruction,
    make_sql_query_tool,
    register_sql_tool,
)


def _call_tool(tool, **kwargs) -> str:
    """Call a FunctionTool's underlying fn directly (no LLM round-trip)."""
    return tool.fn(**kwargs)


class TestSqlQueryRejects:
    @pytest.fixture
    def query_tool(self):
        query_tool, _describe, engine = make_sql_query_tool("sqlite://", row_limit=100)
        yield query_tool
        engine.dispose()

    @pytest.mark.parametrize(
        "stmt",
        [
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET x = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "ALTER TABLE t ADD COLUMN x TEXT",
            "CREATE TABLE t (x TEXT)",
            "TRUNCATE TABLE t",
            "GRANT SELECT ON t TO u",
            "REVOKE ALL ON t FROM u",
            "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN UPDATE SET x=1",
        ],
    )
    def test_rejects_non_select(self, query_tool, stmt):
        result = _call_tool(query_tool, query=stmt)
        assert result.startswith("Error: only SELECT")

    def test_rejects_empty(self, query_tool):
        assert _call_tool(query_tool, query="") == "Error: query must not be empty"
        assert _call_tool(query_tool, query="   ") == "Error: query must not be empty"


class TestSqlQueryAccepts:
    @pytest.fixture
    def query_tool(self):
        query_tool, _describe, engine = make_sql_query_tool("sqlite://", row_limit=100)
        yield query_tool
        engine.dispose()

    def test_select_1(self, query_tool):
        result = _call_tool(query_tool, query="SELECT 1")
        assert json.loads(result) == [{"1": 1}]

    def test_with_cte(self, query_tool):
        result = _call_tool(query_tool, query="WITH x AS (SELECT 1) SELECT * FROM x")
        assert json.loads(result) == [{"1": 1}]

    def test_select_case_insensitive(self, query_tool):
        result = _call_tool(query_tool, query="select 1")
        assert json.loads(result) == [{"1": 1}]

    def test_trailing_semicolon(self, query_tool):
        result = _call_tool(query_tool, query="SELECT 1;")
        assert json.loads(result) == [{"1": 1}]


class TestSqlQueryRowLimit:
    def test_truncates_at_limit(self):
        dsn = "sqlite://"
        query_tool, _describe, engine = make_sql_query_tool(dsn, row_limit=2)
        try:
            result = _call_tool(
                query_tool,
                query="SELECT 1 UNION ALL SELECT 2 UNION ALL SELECT 3 UNION ALL SELECT 4",
            )
            rows = json.loads(result.split("\n")[0])
            assert len(rows) == 2
            assert "truncated at 2 rows" in result
        finally:
            engine.dispose()


class TestSqlQueryCellOmission:
    """Large cells are replaced with an omission marker instead of being
    inlined (or truncated) — the agent has no tool to fetch anything else,
    and a partial fragment would be misleading, so the full value is
    dropped, not shortened."""

    def test_large_string_is_omitted(self):
        big = "x" * 500
        query_tool, _describe, engine = make_sql_query_tool("sqlite://", row_limit=100)
        try:
            result = _call_tool(query_tool, query=f"SELECT '{big}' AS big_col")
            data = json.loads(result)
            cell = data[0]["big_col"]
            assert cell == "<omitted: 500 chars, too large to display>"
            assert "x" * 200 not in cell
        finally:
            engine.dispose()

    def test_small_string_stays_inline(self):
        query_tool, _describe, engine = make_sql_query_tool("sqlite://", row_limit=100)
        try:
            result = _call_tool(query_tool, query="SELECT 'hello' AS small_col")
            data = json.loads(result)
            assert data[0]["small_col"] == "hello"
        finally:
            engine.dispose()


class TestSqlQueryErrorHandling:
    def test_bad_query_returns_error_string(self):
        query_tool, _describe, engine = make_sql_query_tool("sqlite://", row_limit=100)
        try:
            result = _call_tool(query_tool, query="SELECT FROM nonexistent_table")
            assert result.startswith("Error: query failed")
        finally:
            engine.dispose()


class TestDescribeDatabase:
    @pytest.fixture
    def describe_tool(self):
        _query, describe_tool, engine = make_sql_query_tool("sqlite://")
        yield describe_tool
        engine.dispose()

    def test_no_arg_lists_tables(self, describe_tool):
        result = _call_tool(describe_tool)
        data = json.loads(result)
        assert "tables" in data
        assert isinstance(data["tables"], list)

    def test_table_name_returns_columns(self, describe_tool):
        result = _call_tool(describe_tool, table="sqlite_master")
        data = json.loads(result)
        assert data["table"] == "sqlite_master"
        assert "columns" in data
        assert len(data["columns"]) > 0
        col = data["columns"][0]
        assert "name" in col
        assert "type" in col
        assert "nullable" in col
        assert "primary_key" in col

    def test_nonexistent_table_returns_error(self, describe_tool):
        result = _call_tool(describe_tool, table="nonexistent_table_xyz")
        assert result.startswith("Error: could not describe database")


class TestBuildSqlWorkflowInstruction:
    def test_empty_instruction_has_base_text(self):
        text = build_sql_workflow_instruction("")
        assert "describe_database" in text
        assert "sql_query" in text
        assert "Use it when:" not in text

    def test_non_empty_appends_triggers(self):
        text = build_sql_workflow_instruction("look up orders\ncheck status")
        assert "describe_database" in text
        assert "sql_query" in text
        assert "Use it when:" in text
        assert "- look up orders" in text
        assert "- check status" in text


class TestRegisterSqlTool:
    def test_registers_two_tools_and_workflow(self):
        agent = MagicMock()
        engine = register_sql_tool(agent, "sqlite://", instruction="test rules")
        try:
            assert agent.register_tool.call_count == 2
            agent.set_tool_workflow.assert_called_once()
            workflow = agent.set_tool_workflow.call_args[0][0]
            assert "describe_database" in workflow
            assert "sql_query" in workflow
            assert "test rules" in workflow
        finally:
            engine.dispose()
