"""This module discovers and loads cycle-feature CSVs into a partitioned Parquet lake.

Design notes
------------
**Why Parquet in between.** The CSVs are the upstream contract, but they are row
oriented, untyped, and re-written with a new timestamp on every extraction. One
columnar copy, written once and partitioned, means every later query reads only
the columns and partitions it needs.

**Partition on (subject_id, region), not channel.** Partitioning directly on
channel would produce thousands of tiny files for 586 channels across 32
subjects and make the metadata overhead exceed the data. Instead, files are
partitioned by subject and region and *sorted by channel_label* within each
file, so Parquet row-group statistics prune by channel without any small-file
problem. Channel-level predicates still get the pruning benefit, and the file
count stays proportional to subjects x regions.

**Idempotency.** Loading is delete-then-write at partition granularity, keyed on
(subject_id, region, extraction_id). Re-running the same day for the same
partition replaces exactly that directory and leaves the rest of the lake alone,
which is what makes an Airflow retry safe.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Warehouse, sql_string_literal
from .naming import NamingError, SourceFile, is_merged_file, parse_source_path
from .schema import duckdb_columns_struct, duckdb_nullstr_literal


@dataclass(frozen=True)
class DiscoveryResult:
    files: list[SourceFile]
    skipped: list[tuple[str, str]]  # (path, reason)

    @property
    def summary(self) -> dict[str, int]:
        return {
            "discovered": len(self.files),
            "skipped": len(self.skipped),
            "subjects": len({f.subject_id for f in self.files}),
            "partitions": len({f.partition_key for f in self.files}),
        }


def discover(config: Config, subjects: list[str] | None = None) -> DiscoveryResult:
    """Find region-level cycle-feature CSVs under the source root.

    Merged session files and the repository's dtype-placeholder sample file are
    skipped with a recorded reason rather than silently ignored, so that a file
    which should have loaded but did not can be seen in the run record.
    """
    root = config.paths.source_root
    if not root.is_dir():
        raise FileNotFoundError(f"source root does not exist: {root}")

    files: list[SourceFile] = []
    skipped: list[tuple[str, str]] = []

    for path in sorted(root.rglob("*.csv")):
        if is_merged_file(path.name):
            skipped.append((str(path), "merged session file (superset of region files)"))
            continue
        try:
            source = parse_source_path(path, root)
        except NamingError as exc:
            skipped.append((str(path), str(exc)))
            continue
        if subjects and source.subject_id not in subjects:
            skipped.append((str(path), f"subject {source.subject_id} not in requested set"))
            continue
        files.append(source)

    return DiscoveryResult(files=files, skipped=skipped)


def register_source_files(warehouse: Warehouse, run_id: str, discovery: DiscoveryResult) -> None:
    """Record discovered and skipped files for lineage and run health."""
    now = datetime.now(timezone.utc)
    for source in discovery.files:
        warehouse.execute(
            """
            INSERT INTO ops.source_file
                (run_id, source_path, subject_id, region, extraction_id,
                 extracted_at, size_bytes, status, reason, seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', NULL, ?)
            """,
            [
                run_id,
                str(source.path),
                source.subject_id,
                source.region,
                source.extraction_id,
                source.extracted_at,
                source.path.stat().st_size,
                now,
            ],
        )
    for path, reason in discovery.skipped:
        warehouse.execute(
            """
            INSERT INTO ops.source_file
                (run_id, source_path, subject_id, region, extraction_id,
                 extracted_at, size_bytes, status, reason, seen_at)
            VALUES (?, ?, NULL, NULL, NULL, NULL, NULL, 'skipped', ?, ?)
            """,
            [run_id, path, reason, now],
        )


def validate_source_contract(config: Config, discovery: DiscoveryResult) -> list[str]:
    """Cheap pre-load checks on the raw files.

    Reading a header and counting bytes costs almost nothing and catches the
    common upstream failures (a truncated run leaving a header-only file, a
    schema change in bycycle) before anything is written to the lake.

    Returns a list of human-readable problems; empty means the contract holds.
    """
    from .schema import SOURCE_COLUMN_ORDER, missing_columns, unexpected_columns

    problems: list[str] = []
    if not discovery.files:
        problems.append("no cycle-feature CSVs matched the naming convention")

    for source in discovery.files:
        with source.path.open("r", encoding="utf-8-sig", newline="") as handle:
            header_line = handle.readline()
            data_lines = sum(1 for _ in range(config.dq.min_rows_per_file) if handle.readline())

        if not header_line.strip():
            problems.append(f"{source.path.name}: file is empty")
            continue

        observed = [cell.strip() for cell in header_line.rstrip("\r\n").split(",")]
        missing = missing_columns(observed)
        if missing:
            problems.append(f"{source.path.name}: missing columns {missing}")
        extra = unexpected_columns(observed)
        if extra:
            problems.append(f"{source.path.name}: unexpected columns {extra}")
        if len(observed) == len(SOURCE_COLUMN_ORDER) and not missing:
            normalized = [cell.lstrip("\ufeff") for cell in observed]
            if tuple(normalized) != SOURCE_COLUMN_ORDER:
                problems.append(
                    f"{source.path.name}: column order differs from the bycycle contract"
                )
        if data_lines < config.dq.min_rows_per_file:
            problems.append(
                f"{source.path.name}: fewer than {config.dq.min_rows_per_file} data rows"
            )

    return problems


def _partition_dir(config: Config, source: SourceFile) -> Path:
    return (
        config.paths.parquet_root
        / f"subject_id={source.subject_id}"
        / f"region={source.region}"
        / f"extraction_id={source.extraction_id}"
    )


def load_to_lake(
    warehouse: Warehouse,
    config: Config,
    run_id: str,
    discovery: DiscoveryResult,
) -> dict[str, int]:
    """Convert each CSV to a sorted Parquet partition.

    The conversion runs inside DuckDB rather than pandas so that types come from
    the column contract, files larger than memory stream through, and no extra
    dependency (pyarrow) is needed.
    """
    columns_struct = duckdb_columns_struct()
    nullstr = duckdb_nullstr_literal()
    totals = {"partitions": 0, "rows": 0}

    for source in discovery.files:
        target_dir = _partition_dir(config, source)
        # Delete-then-write: makes a retry replace exactly this partition.
        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "part-0.parquet"

        warehouse.execute(
            f"""
            COPY (
                SELECT
                    CAST(trial AS INTEGER)            AS trial,
                    CAST(channel_idx AS INTEGER)      AS channel_idx,
                    CAST(channel_label AS VARCHAR)    AS channel_label,
                    amp_fraction,
                    amp_consistency,
                    period_consistency,
                    monotonicity,
                    period,
                    time_peak,
                    time_trough,
                    volt_peak,
                    volt_trough,
                    time_decay,
                    time_rise,
                    volt_decay,
                    volt_rise,
                    volt_amp,
                    time_rdsym,
                    time_ptsym,
                    band_amp,
                    sample_peak,
                    sample_last_zerox_decay,
                    sample_zerox_decay,
                    sample_zerox_rise,
                    sample_last_trough,
                    sample_next_trough,
                    is_burst,
                    CAST(? AS TIMESTAMP)              AS extracted_at,
                    CAST(? AS VARCHAR)                AS source_file,
                    CAST(? AS VARCHAR)                AS run_id,
                    CAST(? AS TIMESTAMP)              AS loaded_at
                FROM read_csv(
                    ?,
                    header      = true,
                    columns     = {columns_struct},
                    nullstr     = {nullstr},
                    sample_size = -1
                )
                -- Sorting by channel then trial then landmark puts each channel's
                -- cycles in contiguous row groups, so Parquet min/max statistics
                -- prune by channel without partitioning on it.
                ORDER BY channel_label, trial, sample_last_trough
            )
            TO {sql_string_literal(str(target_file))} (FORMAT PARQUET, COMPRESSION ZSTD)
            """,
            [
                source.extracted_at,
                str(source.path),
                run_id,
                datetime.now(timezone.utc),
                str(source.path),
            ],
        )

        rows = warehouse.scalar("SELECT COUNT(*) FROM read_parquet(?)", [str(target_file)]) or 0
        warehouse.execute(
            """
            UPDATE ops.source_file
               SET status = 'loaded', row_count = ?
             WHERE run_id = ? AND source_path = ?
            """,
            [rows, run_id, str(source.path)],
        )
        totals["partitions"] += 1
        totals["rows"] += int(rows)

    return totals


def load_trial_metadata(warehouse: Warehouse, config: Config, run_id: str) -> int:
    """Load per-subject trial metadata (the load condition lives here).

    The cycle-feature CSVs carry no condition column: ``trial`` is only an index.
    The condition comes from ``subjectData.trialinfo``, produced by
    ``defineTrialsStCat`` in the upstream MATLAB pipeline and exported by
    ``matlab/export_trialinfo.m``. Without this table the paired analysis cannot
    be built at all, so a missing directory is an error, not a warning.
    """
    metadata_root = config.paths.trial_metadata
    if not metadata_root.is_dir():
        raise FileNotFoundError(
            f"trial metadata directory not found: {metadata_root}. "
            "Export it with matlab/export_trialinfo.m or generate it with `synth`."
        )

    files = sorted(metadata_root.glob("*_trial_metadata.csv"))
    if not files:
        raise FileNotFoundError(f"no *_trial_metadata.csv files under {metadata_root}")

    warehouse.execute("DELETE FROM core.trial_metadata")
    for path in files:
        warehouse.execute(
            """
            INSERT INTO core.trial_metadata (subject_id, trial, load_condition, correct, run_id)
            SELECT
                CAST(subject_id AS VARCHAR),
                CAST(trial AS INTEGER),
                CAST(load_condition AS INTEGER),
                TRY_CAST(correct AS BOOLEAN),
                CAST(? AS VARCHAR)
            FROM read_csv(?, header = true, sample_size = -1)
            """,
            [run_id, str(path)],
        )

    return int(warehouse.scalar("SELECT COUNT(*) FROM core.trial_metadata") or 0)
