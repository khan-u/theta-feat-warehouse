"""This module is the DuckDB access layer.

DuckDB is used as the warehouse engine because it reads the Parquet lake in
place, supports the window functions and CTEs the marts need, and requires no
server. The DDL is kept to constructs Postgres also accepts (no ``QUALIFY``, no
``ASOF JOIN``) so the same schema can be pointed at Postgres if the project ever
outgrows a single file.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def sql_string_literal(value: str) -> str:
    """Render a Python string as a single-quoted SQL string literal.

    DuckDB requires a constant, not a bound parameter, for the target of
    ``COPY ... TO <target>``. The few call sites that need it pass
    configuration-derived paths, never user input, but embedded single quotes
    are doubled so the literal is always well formed.
    """
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


class SqlRenderError(KeyError):
    """Raised when a .sql file references a placeholder the context lacks."""


def render_sql(sql: str, context: dict[str, str]) -> str:
    """Substitute ``{{name}}`` placeholders.

    Only configuration values (paths, thresholds, condition codes) are ever
    substituted this way. Row values are always passed as bound parameters, so
    this is not a query-building path for user input.
    """

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in context:
            raise SqlRenderError(f"no value supplied for placeholder {{{{{key}}}}}")
        return context[key]

    return PLACEHOLDER.sub(replace, sql)


def split_statements(sql: str) -> list[str]:
    """Split a script into statements, ignoring semicolons inside literals.

    DuckDB's Python API executes multiple statements in one call, but splitting
    means a failure reports which statement failed rather than the whole file.
    """
    statements: list[str] = []
    buffer: list[str] = []
    in_single = in_double = in_line_comment = in_block_comment = False
    index = 0
    while index < len(sql):
        char = sql[index]
        pair = sql[index : index + 2]

        if in_line_comment:
            buffer.append(char)
            if char == "\n":
                in_line_comment = False
            index += 1
            continue
        if in_block_comment:
            buffer.append(char)
            if pair == "*/":
                buffer.append(sql[index + 1])
                in_block_comment = False
                index += 2
                continue
            index += 1
            continue
        if not in_single and not in_double:
            if pair == "--":
                in_line_comment = True
                buffer.append(pair)
                index += 2
                continue
            if pair == "/*":
                in_block_comment = True
                buffer.append(pair)
                index += 2
                continue

        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double

        if char == ";" and not in_single and not in_double:
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue

        buffer.append(char)
        index += 1

    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


class Warehouse:
    """Thin wrapper around a DuckDB connection."""

    def __init__(self, database_path: Path, sql_context: dict[str, str], read_only: bool = False):
        import duckdb  # imported lazily so the module imports without the engine

        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path
        self.sql_context = sql_context
        self.connection = duckdb.connect(str(database_path), read_only=read_only)
        self.connection.execute("SET TimeZone = 'UTC'")

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Warehouse":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ---------------------------------------------------------------- execution

    def execute(self, sql: str, parameters: Sequence[Any] | None = None) -> Any:
        rendered = render_sql(sql, self.sql_context)
        return self.connection.execute(rendered, parameters or [])

    def execute_script(self, path: Path) -> list[str]:
        """Run every statement in a .sql file; return the statements executed."""
        script = path.read_text(encoding="utf-8")
        executed: list[str] = []
        for statement in split_statements(script):
            try:
                self.execute(statement)
            except Exception as exc:  # pragma: no cover - surfaced to the caller
                preview = " ".join(statement.split())[:180]
                raise RuntimeError(f"{path.name}: statement failed: {preview}") from exc
            executed.append(statement)
        return executed

    def scalar(self, sql: str, parameters: Sequence[Any] | None = None) -> Any:
        row = self.execute(sql, parameters).fetchone()
        return None if row is None else row[0]

    def rows(self, sql: str, parameters: Sequence[Any] | None = None) -> list[tuple[Any, ...]]:
        return self.execute(sql, parameters).fetchall()

    # ------------------------------------------------------------ run registry

    def start_run(self, run_id: str | None = None, triggered_by: str = "cli") -> str:
        """Open a pipeline_run row and return its id.

        Airflow passes its own ``run_id`` so a warehouse row can be traced back
        to the DAG run that produced it; the CLI generates a UUID.
        """
        run_id = run_id or f"local__{uuid.uuid4().hex[:12]}"
        self.execute(
            """
            INSERT INTO ops.pipeline_run (run_id, triggered_by, started_at, status)
            VALUES (?, ?, ?, 'running')
            ON CONFLICT (run_id) DO UPDATE SET
                started_at = excluded.started_at,
                status     = 'running',
                finished_at = NULL,
                message    = NULL
            """,
            [run_id, triggered_by, datetime.now(timezone.utc)],
        )
        return run_id

    def finish_run(self, run_id: str, status: str, message: str | None = None) -> None:
        self.execute(
            """
            UPDATE ops.pipeline_run
               SET status = ?, finished_at = ?, message = ?
             WHERE run_id = ?
            """,
            [status, datetime.now(timezone.utc), message, run_id],
        )
