"""This module is the data-quality gate for the FASTQ read-metrics lake.

It reuses the ``Check``/``CheckOutcome`` types from the theta gate so both paths
report quality the same way: a scalar SQL query, a comparison and a threshold,
with ``error`` failing the run and ``warn`` only recording. The checks run over
the per-read Parquet, read in place with ``read_parquet``.

The gate is where FAIR "validated before any conclusion" applies to the genomics
format: a read length must be positive, Phred scores must sit in the range the
configured encoding allows, and GC content must be a real fraction. Malformed
reads (quality/sequence length mismatch, out-of-alphabet bases) never reach the
lake because ``ingest_fastq`` drops them at parse time, so these checks are the
second line, catching an encoding or mapping error rather than a corrupt read.
"""

from __future__ import annotations

from pathlib import Path

from .config import Genomics
from .db import sql_string_literal
from .dq import Check, CheckOutcome, DataQualityError
from .fastq_source import READ_METRICS_COLUMNS


def build_fastq_checks(genomics: Genomics) -> list[Check]:
    """Assemble the read-metrics check suite from genomics settings."""
    return [
        Check(
            name="fastq_not_empty",
            severity="error",
            sql="SELECT COUNT(*) FROM reads",
            comparison=">=",
            threshold=float(genomics.min_reads),
            detail="Read-metrics lake has too few reads; ingestion produced nothing usable.",
        ),
        Check(
            name="read_length_positive",
            severity="error",
            sql="SELECT COUNT(*) FROM reads WHERE seq_length <= 0",
            comparison="==",
            threshold=0.0,
            detail="A read has non-positive length, which a valid FASTQ record cannot.",
        ),
        Check(
            name="phred_in_range",
            severity="error",
            sql=f"SELECT COUNT(*) FROM reads WHERE min_phred < 0 OR max_phred > {genomics.phred_max}",
            comparison="==",
            threshold=0.0,
            detail=(
                f"A Phred score falls outside [0, {genomics.phred_max}] for the "
                f"configured offset ({genomics.phred_offset}). Most likely the "
                "quality encoding does not match phred_offset."
            ),
        ),
        Check(
            name="gc_content_in_unit_interval",
            severity="error",
            sql="SELECT COUNT(*) FROM reads WHERE gc_content < 0 OR gc_content > 1",
            comparison="==",
            threshold=0.0,
            detail="GC content is a fraction on [0, 1]; a value outside it means a mapping error.",
        ),
        Check(
            name="mean_quality_reasonable",
            severity="warn",
            sql="SELECT COALESCE(AVG(mean_phred), 0.0) FROM reads",
            comparison=">=",
            threshold=20.0,
            detail=(
                "Mean base quality is low (below Phred 20, a 1-in-100 error rate). "
                "Worth confirming the reads were not left unfiltered or mis-encoded."
            ),
        ),
    ]


def _connect_reads(lake_root: Path):
    """Open a DuckDB connection exposing the lake as a ``reads`` relation.

    When the lake has no Parquet yet, an empty typed table stands in so the
    checks still run and ``fastq_not_empty`` fails cleanly rather than the query
    erroring on a missing file.
    """
    import duckdb

    connection = duckdb.connect()
    connection.execute("SET TimeZone = 'UTC'")
    parquet_files = sorted(Path(lake_root).rglob("*.parquet"))
    if parquet_files:
        # read_parquet inside CREATE VIEW cannot take a bound parameter, so the
        # file list is inlined as a SQL literal. The paths are config-derived,
        # never user input, and any embedded quote is escaped by the helper.
        file_list = "[" + ", ".join(sql_string_literal(str(p)) for p in parquet_files) + "]"
        connection.execute(
            f"CREATE VIEW reads AS SELECT * FROM read_parquet({file_list}, hive_partitioning = true)"
        )
    else:
        ddl = ", ".join(f"{name} {sql_type}" for name, sql_type in READ_METRICS_COLUMNS)
        connection.execute(f"CREATE TABLE reads ({ddl})")
    return connection


def run_fastq_checks(
    genomics: Genomics,
    lake_root: Path,
    raise_on_error: bool = True,
) -> list[CheckOutcome]:
    """Execute the suite against the read-metrics lake and optionally fail."""
    outcomes: list[CheckOutcome] = []

    connection = _connect_reads(lake_root)
    try:
        for check in build_fastq_checks(genomics):
            try:
                row = connection.execute(check.sql).fetchone()
                observed = None if row is None else row[0]
            except Exception:
                observed = None
            observed_value = None if observed is None else float(observed)
            outcomes.append(
                CheckOutcome(check=check, observed=observed_value, passed=check.evaluate(observed_value))
            )
    finally:
        connection.close()

    blocking = [o for o in outcomes if o.is_blocking]
    if blocking and raise_on_error:
        lines = [
            f"  - {o.check.name}: observed={o.observed} "
            f"{o.check.comparison} {o.check.threshold} failed. {o.check.detail}"
            for o in blocking
        ]
        raise DataQualityError(
            f"{len(blocking)} FASTQ data-quality check(s) failed:\n" + "\n".join(lines)
        )

    return outcomes
