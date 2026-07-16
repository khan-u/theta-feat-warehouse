"""This module exports mart tables as Tableau-ready CSV extracts.

Tableau connects to the DuckDB file only through a JDBC/ODBC driver that not
every install has, and Tableau Public cannot connect to a local database at all.
Flat CSV extracts work in every Tableau edition, load fast because the marts are
already aggregated, and double as the diff-able artifact that shows what changed
between runs.

Each extract is written with the run_id embedded so a dashboard refresh can be
tied to the pipeline run that produced it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import Config
from .db import Warehouse, sql_string_literal

# (extract name, source relation, ORDER BY clause)
EXTRACTS: tuple[tuple[str, str, str], ...] = (
    (
        "channel_paired_asym",
        "mart.channel_paired_asym",
        "subject_id, region, channel_label",
    ),
    (
        "channel_load_asym",
        "mart.channel_load_asym",
        "subject_id, region, channel_label, load_condition",
    ),
    (
        "cycle_qc_distribution",
        "mart.cycle_qc_distribution",
        "metric, region, load_condition, is_burst, bin_start",
    ),
    (
        "subject_coverage",
        "mart.subject_coverage",
        "subject_id, region",
    ),
    (
        "run_health",
        "mart.run_health",
        "started_at DESC",
    ),
    (
        "test_results",
        "ops.test_result",
        "metric, test",
    ),
    (
        "dq_results",
        "ops.dq_result",
        "checked_at DESC, check_name",
    ),
)


def export_extracts(warehouse: Warehouse, config: Config, run_id: str) -> dict[str, int]:
    """Write one CSV per extract plus a manifest; return row counts."""
    export_dir = config.paths.export_dir
    export_dir.mkdir(parents=True, exist_ok=True)

    counts: dict[str, int] = {}
    for name, relation, order_by in EXTRACTS:
        target = export_dir / f"{name}.csv"
        warehouse.execute(
            f"""
            COPY (
                SELECT *, CAST(? AS VARCHAR) AS exported_for_run_id
                FROM {relation}
                ORDER BY {order_by}
            )
            TO {sql_string_literal(str(target))} (FORMAT CSV, HEADER, DELIMITER ',')
            """,
            [run_id],
        )
        counts[name] = int(warehouse.scalar(f"SELECT COUNT(*) FROM {relation}") or 0)

    manifest = {
        "run_id": run_id,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "duckdb_path": str(config.paths.duckdb_path),
        "row_counts": counts,
        "analysis": {
            "burst_only": config.analysis.burst_only,
            "baseline_condition": config.analysis.baseline_condition,
            "comparison_condition": config.analysis.comparison_condition,
            "min_cycles_per_channel_load": config.analysis.min_cycles_per_channel_load,
            "n_permutations": config.analysis.n_permutations,
            "random_seed": config.analysis.random_seed,
        },
        "signal": {
            "fs": config.signal.fs,
            "f_theta": [config.signal.f_theta_low, config.signal.f_theta_high],
            "f_lowpass": config.signal.f_lowpass,
            "epoch_window_s": [config.signal.epoch_start_s, config.signal.epoch_end_s],
        },
    }
    (export_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    return counts
