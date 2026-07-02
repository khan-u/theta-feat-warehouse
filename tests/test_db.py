import pytest

from theta_warehouse.db import SqlRenderError, render_sql, split_statements, sql_string_literal


def test_sql_string_literal_escapes_single_quotes():
    assert sql_string_literal("it's") == "'it''s'"


def test_sql_string_literal_plain_string():
    assert sql_string_literal("/some/path") == "'/some/path'"


def test_render_sql_substitutes_placeholders():
    result = render_sql("SELECT {{ col }} FROM {{ table }}", {"col": "x", "table": "t"})
    assert result == "SELECT x FROM t"


def test_render_sql_raises_on_missing_key():
    with pytest.raises(SqlRenderError):
        render_sql("SELECT {{missing}}", {})


def test_split_statements_basic():
    sql = "SELECT 1; SELECT 2;"
    parts = split_statements(sql)
    assert parts == ["SELECT 1", "SELECT 2"]


def test_split_statements_ignores_semicolon_in_string_literal():
    sql = "SELECT 'a;b' AS x; SELECT 2"
    parts = split_statements(sql)
    assert len(parts) == 2
    assert "a;b" in parts[0]


def test_split_statements_ignores_semicolon_in_line_comment():
    sql = "-- comment; still a comment\nSELECT 1"
    parts = split_statements(sql)
    assert len(parts) == 1
