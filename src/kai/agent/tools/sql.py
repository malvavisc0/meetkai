"""sql_query + describe_database — read-only SQL over the operator's external database.

Bot-agnostic: ``cli/bot.py:_start()`` reads ``SqlSettings`` from env vars
(``KAI_SQL_DSN`` / ``KAI_SQL_INSTRUCTION``), and when ``sql_enabled`` is
true, calls :func:`register_sql_tool`, which registers both tools on the
agent AND injects a workflow-guidance block into the system prompt.

The workflow instruction composes — it's appended to any existing workflow
blocks (the waha bot's web-search workflow, the Brain's workflow) rather
than replacing them (``agent/core.py:set_tool_workflow``).

Neither tool function contains ``logger.info`` call/result logging — that
is handled generically by ``agent/core.py:_run_with_tools`` for every
registered tool. The DSN lives only in the ``engine`` closure and is
never passed to any logger.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol

import sqlalchemy as sa
from llama_index.core.tools import FunctionTool
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Cells larger than this are omitted from the JSON result — the agent has
# no tool to act on a large blob anyway, and a truncated fragment is worse
# than nothing: it looks like real data but isn't, inviting the model to
# draw conclusions from a partial value. A short marker with the original
# length tells the model the column has data without the misleading partial
# content or the context cost of a multi-KB string.
_MAX_INLINE_CHARS = 200


def _omit_large_cell(val: object) -> object:
    """Inline small values; replace oversized string/bytes cells with a marker."""
    if isinstance(val, str) and len(val) > _MAX_INLINE_CHARS:
        return f"<omitted: {len(val)} chars, too large to display>"
    if isinstance(val, (bytes, bytearray)):
        return f"<omitted: {len(val)} bytes, binary>"
    return val


_SELECT_RE = re.compile(r"^\s*(?:WITH\s+.*?\s+)?SELECT\b", re.IGNORECASE | re.DOTALL)
_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge)\b",
    re.IGNORECASE,
)

# Sets a 30s statement timeout on every Postgres connection at checkout,
# so a runaway query can't hang the bot. Other dialects (SQLite, MySQL) get
# no timeout — their drivers either don't support it or default short enough.
_QUERY_TIMEOUT_SECONDS = 30


def _create_sql_engine(dsn: str) -> sa.Engine:
    """Build a SQLAlchemy engine with dialect-appropriate query timeout.

    For PostgreSQL, the statement timeout is set via ``connect_args.options``
    (the server-side ``-c statement_timeout`` flag) so it applies to every
    statement on every pooled connection — no per-query ``SET LOCAL`` needed.
    A ``connect_timeout`` is also set so a dead/unreachable host fails fast
    instead of hanging on the OS TCP timeout.
    """
    if dsn.startswith("postgresql"):
        return sa.create_engine(
            dsn,
            connect_args={
                "options": f"-c statement_timeout={_QUERY_TIMEOUT_SECONDS * 1000}",
                "connect_timeout": 10,
            },
        )
    return sa.create_engine(dsn)


class SqlSettings(BaseSettings):
    """SQL tool settings — read from KAI_SQL_* env vars (injected by the cockpit)."""

    model_config = SettingsConfigDict(env_prefix="KAI_SQL_", env_file=".env", extra="ignore")

    dsn: str = ""
    instruction: str = ""
    row_limit: int = 100

    @property
    def sql_enabled(self) -> bool:
        return bool(self.dsn)


def get_sql_settings() -> SqlSettings:
    return SqlSettings()


class _SqlToolAgent(Protocol):
    def register_tool(self, tool: FunctionTool) -> None: ...

    def set_tool_workflow(self, workflow: str | None) -> None: ...


def build_sql_workflow_instruction(instruction: str) -> str:
    """Render the operator's SQL usage rules into a workflow prompt block.

    Empty instruction = a minimal default that just tells the agent the tool
    exists and is read-only. Non-empty = the operator's free-text rules,
    one trigger per line, appended as guidance.
    """
    base = (
        "You have two tools for the operator's connected database:\n"
        "- `describe_database` — list all tables (no argument) or show "
        "columns for one table (pass the table name). Call this first to "
        "learn the schema before writing queries.\n"
        "- `sql_query` — run a read-only SELECT / WITH ... SELECT query and "
        "get rows as JSON. Only SELECT is allowed; writes are rejected. "
        "Results are capped at a row limit."
    )
    triggers = [ln.strip() for ln in instruction.splitlines() if ln.strip()]
    if not triggers:
        return base
    body = "\n".join(f"- {ln}" for ln in triggers)
    return f"{base}\nUse it when:\n{body}"


def make_describe_database_tool(engine: sa.Engine) -> FunctionTool:
    """Schema introspection — lets the agent discover tables and columns."""

    def describe_database(table: str | None = None) -> str:
        """Describe the database structure.

        Pass no argument to list all table names. Pass a table name to get
        its columns (name, type, nullable, primary key) as JSON. Call this
        before writing queries so you know the correct table and column
        names.

        Args:
            table: Optional table name. If omitted, lists all tables.
        """
        try:
            inspector = sa.inspect(engine)
            if table is None:
                tables = sorted(inspector.get_table_names())
                return json.dumps({"tables": tables})
            cols = []
            pk = set(inspector.get_pk_constraint(table).get("constrained_columns", []))
            for col in inspector.get_columns(table):
                cols.append(
                    {
                        "name": col["name"],
                        "type": str(col["type"]),
                        "nullable": col.get("nullable", True),
                        "primary_key": col["name"] in pk,
                    }
                )
            return json.dumps({"table": table, "columns": cols}, default=str)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result
            logger.exception("describe_database failed")
            return f"Error: could not describe database ({exc})"

    return FunctionTool.from_defaults(
        fn=describe_database,
        name="describe_database",
        description=(
            "List all tables in the database (no argument), or show the "
            "columns of one table (pass its name). Use before sql_query to "
            "learn the schema."
        ),
    )


def make_sql_query_tool(
    dsn: str,
    *,
    row_limit: int = 100,
) -> tuple[FunctionTool, FunctionTool, sa.Engine]:
    """Build the ``sql_query`` + ``describe_database`` tools bound to ``engine``.

    Returns ``(query_tool, describe_tool, engine)`` so the caller can
    dispose the engine on shutdown.
    """
    engine = _create_sql_engine(dsn)

    def sql_query(query: str) -> str:
        """Run a read-only SQL query against the operator's database.

        Use for retrieving rows from the connected database. Only SELECT /
        WITH ... SELECT statements are allowed; writes are rejected. Results
        are capped at a row limit and returned as JSON.

        Args:
            query: A single SELECT (or WITH ... SELECT) statement.
        """
        q = (query or "").strip().rstrip(";")
        if not q:
            return "Error: query must not be empty"
        if _FORBIDDEN.search(q) or not _SELECT_RE.match(q):
            return "Error: only SELECT / WITH ... SELECT statements are allowed"
        try:
            with engine.connect() as conn:
                result = conn.execute(sa.text(q))
                rows = result.fetchmany(row_limit + 1)
                truncated = len(rows) > row_limit
                rows = rows[:row_limit]
                cols = list(result.keys())
                out = [
                    {col: _omit_large_cell(val) for col, val in zip(cols, row, strict=False)}
                    for row in rows
                ]
            text = json.dumps(out, default=str)
            if truncated:
                text += f"\n(truncated at {row_limit} rows)"
            return text
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a tool result
            logger.exception("sql_query failed")
            return f"Error: query failed ({exc})"

    tool = FunctionTool.from_defaults(
        fn=sql_query,
        name="sql_query",
        description=(
            "Run a read-only SELECT query against the operator's connected "
            "database and return rows as JSON. Only SELECT / WITH ... SELECT "
            "is allowed; writes are rejected. Results are capped."
        ),
    )
    describe_tool = make_describe_database_tool(engine)
    return tool, describe_tool, engine


def register_sql_tool(
    agent: _SqlToolAgent,
    dsn: str,
    *,
    instruction: str = "",
    row_limit: int = 100,
) -> sa.Engine:
    """Register describe_database + sql_query on agent and inject the
    workflow prompt. Returns the engine so the caller can dispose it on
    shutdown.
    """
    query_tool, describe_tool, engine = make_sql_query_tool(dsn, row_limit=row_limit)
    agent.register_tool(describe_tool)
    agent.register_tool(query_tool)
    agent.set_tool_workflow(build_sql_workflow_instruction(instruction))
    return engine
