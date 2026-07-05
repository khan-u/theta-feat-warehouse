"""Smoke tests for the SQL DDL layer."""
from pathlib import Path

import duckdb
import pytest

from theta_warehouse.db import render_sql, split_statements

SQL_DIR = Path(__file__).parent.parent / "sql"


def test_ops_ddl_creates_expected_tables():
    con = duckdb.connect(":memory:")
    script = (SQL_DIR / "010_ops.sql").read_text(encoding="utf-8")
    for stmt in split_statements(script):
        con.execute(stmt)
    tables = {row[0] for row in con.execute(
        "SELECT table_schema || '.' || table_name FROM information_schema.tables"
    ).fetchall()}
    assert "ops.pipeline_run" in tables
    assert "ops.source_file" in tables
    assert "ops.dq_result" in tables
    assert "ops.test_result" in tables
    assert "core.trial_metadata" in tables
    con.close()


def test_all_sql_files_parse_without_error():
    for sql_file in sorted(SQL_DIR.glob("*.sql")):
        statements = split_statements(sql_file.read_text(encoding="utf-8"))
        assert len(statements) > 0, f"{sql_file.name} yielded no statements"


def test_mart_sql_renders_with_context():
    context = {
        "parquet_root": "/tmp/lake",
        "export_dir": "/tmp/exports",
        "fs": "400",
        "burst_only": "TRUE",
        "baseline_condition": "1",
        "comparison_condition": "3",
        "min_cycles_per_channel_load": "10",
        "symmetry_null_value": "0.5",
        "symmetry_lower_bound": "0.0",
        "symmetry_upper_bound": "1.0",
    }
    for sql_file in sorted(SQL_DIR.glob("0[234]*.sql")):
        rendered = render_sql(sql_file.read_text(encoding="utf-8"), context)
        assert "{{" not in rendered, f"unresolved placeholder in {sql_file.name}"
