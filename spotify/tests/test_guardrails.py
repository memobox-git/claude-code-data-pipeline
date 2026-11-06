"""The agent can read gold and nothing else, whatever SQL the model writes."""
import json

import psycopg
import pytest

from spotify.common import agent_connect
from spotify.tools import GoldTools, ToolError, check_read_only

from .conftest import needs_agent_db


# ---------------------------------------------------------------- statement filter (no database)
@pytest.mark.parametrize("query", [
    "SELECT 1", "select 1;", "  WITH x AS (SELECT 1) SELECT * FROM x",
    "SELECT ';' AS semicolon_in_a_string", "SELECT 1 -- trailing; comment",
    "SELECT artists FROM gold.dim_track WHERE artists LIKE '%;%'",
])
def test_filter_allows_single_reads(query):
    assert check_read_only(query)


@pytest.mark.parametrize("query", [
    "DELETE FROM gold.dim_track", "DROP TABLE gold.dim_track", "UPDATE gold.dim_track SET popularity = 0",
    "SELECT 1; DROP TABLE gold.dim_track", "SELECT 1; COMMIT; SET statement_timeout = 0",
    "/* hi */ INSERT INTO gold.dim_genre VALUES ('x', 1, 1, 'x')", "COPY gold.dim_track TO STDOUT",
])
def test_filter_rejects_writes_and_multiple_statements(query):
    with pytest.raises(ToolError):
        check_read_only(query)


# ---------------------------------------------------------------- against the database
@pytest.fixture
def tools():
    t = GoldTools()
    yield t
    t.close()


def _run(tools, query):
    return json.loads(tools.run_sql(query, "test"))


@needs_agent_db
def test_reads_gold(tools):
    assert _run(tools, "SELECT count(*) FROM gold.dim_track")["rows"][0][0] > 0


@needs_agent_db
def test_cannot_see_bronze_silver_or_meta(tools):
    for table in ("bronze.tracks", "silver.tracks", "meta.dq_results"):
        with pytest.raises(ToolError, match="permission denied"):
            tools.run_sql(f"SELECT count(*) FROM {table}")


@needs_agent_db
def test_write_hidden_in_a_cte_is_blocked(tools):
    before = _run(tools, "SELECT count(*) FROM gold.dim_track")["rows"][0][0]
    with pytest.raises(ToolError, match="read-only"):
        tools.run_sql("WITH d AS (DELETE FROM gold.dim_track RETURNING 1) SELECT count(*) FROM d")
    assert _run(tools, "SELECT count(*) FROM gold.dim_track")["rows"][0][0] == before


@pytest.fixture
def read_write_session():
    """An agent_reader session that turned read-only mode off, as a determined attacker could."""
    with agent_connect() as conn:
        conn.autocommit = True  # each statement in its own transaction, started read-write
        conn.execute("SET default_transaction_read_only = off")
        yield conn


@needs_agent_db
def test_role_has_no_write_privilege_even_outside_read_only_mode(read_write_session):
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_write_session.execute("INSERT INTO gold.dim_genre VALUES ('x', 1, 1, 'x')")
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        read_write_session.execute("TRUNCATE gold.dim_track")


@needs_agent_db
def test_cannot_create_tables_or_switch_role(read_write_session):
    for statement in ("CREATE TEMP TABLE t (x int)", "CREATE TABLE gold.t (x int)", "SET ROLE postgres"):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            read_write_session.execute(statement)


@needs_agent_db
def test_row_cap(tools):
    out = _run(tools, "SELECT track_id FROM gold.dim_track")
    assert out["row_count"] == 200 and out["truncated"] is True


@needs_agent_db
def test_statement_timeout():
    t = GoldTools(timeout="500ms")
    try:
        with pytest.raises(ToolError, match="timeout"):
            t.run_sql("SELECT pg_sleep(2)")
        assert json.loads(t.run_sql("SELECT 1"))["rows"] == [[1]]  # the connection recovers
    finally:
        t.close()


@needs_agent_db
def test_every_run_is_recorded(tools):
    tools.run_sql("SELECT 1", "ok")
    with pytest.raises(ToolError):
        tools.run_sql("DELETE FROM gold.dim_track", "bad")
    assert [(r.purpose, r.error is None) for r in tools.runs] == [("ok", True), ("bad", False)]


@needs_agent_db
def test_list_and_describe(tools):
    tables = {t["table"]: t for t in json.loads(tools.list_tables())}
    assert "Grain: one row = one track" in tables["gold.dim_track"]["grain"]
    desc = json.loads(tools.describe_table("dim_track"))
    assert {"popularity", "explicit", "tempo_bpm"} <= {c["name"] for c in desc["columns"]}
    for bad in ("silver.tracks", "dim_track; DROP TABLE gold.dim_track", "pg_authid"):
        with pytest.raises(ToolError, match="Unknown table"):
            tools.describe_table(bad)


@needs_agent_db
def test_every_agreed_metric_runs_as_the_agent(tools):
    for name, spec in tools.metrics.items():
        for key in ("sql", "bridge_sql"):
            if key in spec:
                assert _run(tools, spec[key])["row_count"] > 0, f"{name}.{key}"


def test_unknown_metric_lists_known_ones():
    t = GoldTools(connect=lambda: None)
    content, is_error = t.call("get_metric", {"name": "revenue"})
    assert is_error and "catalog_tracks" in content
