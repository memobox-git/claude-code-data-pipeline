"""The agent's four tools. All of them read gold through the read-only agent_reader login.

Defense in depth for run_sql:
  1. the tool accepts one SELECT / WITH statement (no ';' outside quotes and comments)
  2. every query runs in a READ ONLY transaction with a statement timeout
  3. the database login can SELECT on gold and nothing else (sql/40_agent_role.sql)
Layer 3 is the real guarantee; 1 and 2 give the model clear errors it can recover from.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

import psycopg
import yaml
from psycopg import sql

from .common import PKG, agent_connect, env

MAX_ROWS = 200
MAX_CELL = 300
METRICS_PATH = PKG / "metrics.yaml"

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_tables",
        "description": "List the tables in the gold schema with their row counts and grain "
                       "(what one row represents). Gold is the only data you can read.",
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
    {
        "name": "describe_table",
        "description": "Show one gold table's columns (name, type, meaning) and 3 sample rows. "
                       "Use it before writing SQL against a table you have not described yet.",
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "Table name in the gold schema, e.g. dim_track"}},
            "required": ["table"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "get_metric",
        "description": "Get the agreed definition of a business metric: what it means, its grain, the SQL "
                       "that computes it, and caveats to mention. Use it whenever a question touches one "
                       "of the agreed metrics, and compute the metric exactly that way.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Metric name, e.g. catalog_tracks"}},
            "required": ["name"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "run_sql",
        "description": "Run one read-only PostgreSQL SELECT against the gold schema and get the result as "
                       f"JSON (at most {MAX_ROWS} rows). Queries time out after a few seconds, so aggregate "
                       "in SQL rather than pulling raw rows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "One SELECT or WITH ... SELECT statement. "
                                                         "Qualify tables with the schema, e.g. gold.dim_track."},
                "purpose": {"type": "string", "description": "A few words on what this query checks; "
                                                             "shown to the user next to the SQL."},
            },
            "required": ["sql", "purpose"], "additionalProperties": False,
        },
        "strict": True,
    },
]

# Strings, quoted identifiers, comments and dollar-quoted bodies may contain ';' legitimately.
_NOISE = re.compile(r"""'(?:[^']|'')*' | "(?:[^"]|"")*" | --[^\n]* | /\*.*?\*/ | \$(\w*)\$.*?\$\1\$""", re.S | re.X)
_READ = re.compile(r"^\s*(select|with)\b", re.I)


class ToolError(Exception):
    """A problem the model should see and can fix (bad table name, rejected SQL, SQL error)."""


@dataclass
class SqlRun:
    sql: str
    purpose: str
    row_count: int | None
    error: str | None
    ms: int


def load_metrics() -> dict:
    return yaml.safe_load(METRICS_PATH.read_text())


def check_read_only(query: str) -> str:
    """Return the single statement to run, or raise ToolError explaining why not."""
    stmt = query.strip().rstrip(";").strip()
    body = _NOISE.sub(" ", stmt)
    if not _READ.match(body):
        raise ToolError("Only a SELECT (or WITH ... SELECT) statement is allowed. The data is read-only.")
    if ";" in body:
        raise ToolError("Send one statement at a time: no ';' inside the query.")
    return stmt


def _cell(v: Any) -> Any:
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, str):
        return v if len(v) <= MAX_CELL else v[:MAX_CELL] + "..."
    if v is None or isinstance(v, (int, bool)):
        return v
    return str(v)  # dates, timestamps, anything else


def _pg_error(e: psycopg.Error) -> str:
    diag = getattr(e, "diag", None)
    msg = (diag.message_primary if diag and diag.message_primary else str(e)).strip()
    hint = diag.message_hint if diag and diag.message_hint else ""
    return f"SQL error: {msg}" + (f" (hint: {hint})" if hint else "")


class GoldTools:
    """Runs the tools for one agent. Keeps a record of every SQL run for the log and the evals."""

    def __init__(self, connect: Callable[[], psycopg.Connection] = agent_connect, metrics: dict | None = None,
                 timeout: str | None = None):
        self._connect = connect
        self._conn: psycopg.Connection | None = None
        self.metrics = metrics if metrics is not None else load_metrics()
        self.timeout = timeout or env("AGENT_STATEMENT_TIMEOUT", "15s")
        self.runs: list[SqlRun] = []

    # ------------------------------------------------------------------ plumbing
    def _db(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect()
            self._conn.autocommit = True  # each query gets its own explicit transaction below
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()

    def _query(self, query: str | sql.Composable, params: tuple | None = None, limit: int = MAX_ROWS):
        conn = self._db()
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ ONLY")
            cur.execute(sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(self.timeout)))
            cur.execute(query, params)
            if cur.description is None:
                return [], []
            return [d.name for d in cur.description], cur.fetchmany(limit + 1)

    def _gold_tables(self) -> dict[str, str]:
        _, rows = self._query(
            "SELECT c.relname, coalesce(obj_description(c.oid, 'pg_class'), '') FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'gold' AND c.relkind IN ('r', 'v', 'm', 'p') ORDER BY 1", limit=1000)
        return dict(rows)

    # ------------------------------------------------------------------ tools
    def call(self, name: str, args: dict) -> tuple[str, bool]:
        """Run one tool call. Returns (content, is_error)."""
        try:
            if name == "list_tables":
                return self.list_tables(), False
            if name == "describe_table":
                return self.describe_table(args["table"]), False
            if name == "get_metric":
                return self.get_metric(args["name"]), False
            if name == "run_sql":
                return self.run_sql(args["sql"], args.get("purpose", "")), False
            return f"Unknown tool: {name}", True
        except ToolError as e:
            return str(e), True
        except psycopg.Error as e:
            return _pg_error(e), True
        except (KeyError, TypeError) as e:
            return f"Bad arguments for {name}: {e}", True

    def list_tables(self) -> str:
        out = []
        for table, grain in self._gold_tables().items():
            _, rows = self._query(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("gold", table)))
            out.append({"table": f"gold.{table}", "rows": rows[0][0], "grain": grain})
        return json.dumps(out, indent=1)

    def describe_table(self, table: str) -> str:
        name = table.strip().removeprefix("gold.")
        tables = self._gold_tables()
        if name not in tables:
            raise ToolError(f"Unknown table '{table}'. Gold tables: {', '.join(tables)}")
        _, cols = self._query(
            "SELECT a.attname, format_type(a.atttypid, a.atttypmod), coalesce(col_description(a.attrelid, a.attnum), '') "
            "FROM pg_attribute a WHERE a.attrelid = %s::regclass AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum", (f"gold.{name}",), limit=1000)
        sample_cols, sample = self._query(sql.SQL("SELECT * FROM {} LIMIT 3").format(sql.Identifier("gold", name)))
        return json.dumps({
            "table": f"gold.{name}", "grain": tables[name],
            "columns": [{"name": c, "type": t, "meaning": m} for c, t, m in cols],
            "sample_rows": {"columns": sample_cols, "rows": [[_cell(v) for v in r] for r in sample]},
        }, indent=1)

    def get_metric(self, name: str) -> str:
        spec = self.metrics.get(name.strip())
        if spec is None:
            raise ToolError(f"Unknown metric '{name}'. Agreed metrics: {', '.join(self.metrics)}")
        return json.dumps({"metric": name.strip(), **spec}, indent=1)

    def run_sql(self, query: str, purpose: str = "") -> str:
        started = time.monotonic()
        row_count, error, stmt = None, None, query.strip()
        try:
            stmt = check_read_only(query)
            cols, rows = self._query(stmt)
            row_count = min(len(rows), MAX_ROWS)
            return json.dumps({"columns": cols, "rows": [[_cell(v) for v in r] for r in rows[:MAX_ROWS]],
                               "row_count": row_count, "truncated": len(rows) > MAX_ROWS}, default=str)
        except ToolError as e:
            error = str(e)
            raise
        except psycopg.Error as e:
            error = _pg_error(e)
            raise ToolError(error) from e
        finally:
            self.runs.append(SqlRun(stmt, purpose, row_count, error, int((time.monotonic() - started) * 1000)))
