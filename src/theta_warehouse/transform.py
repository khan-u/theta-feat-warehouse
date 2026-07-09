"""This module handles SQL orchestration and runs the statistical analysis step."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .db import Warehouse
from .stats import TestResult, holm_bonferroni, one_sample_permutation_test, paired_permutation_test

SQL_DIR = Path(__file__).resolve().parents[2] / "sql"


def sql_path(name: str) -> Path:
    path = SQL_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"SQL script not found: {path}")
    return path


def init_schema(warehouse: Warehouse) -> None:
    """Create schemas, ops tables and reference tables."""
    warehouse.execute_script(sql_path("010_ops.sql"))


def build_staging(warehouse: Warehouse) -> None:
    warehouse.execute_script(sql_path("020_stage.sql"))


def build_core(warehouse: Warehouse) -> dict[str, int]:
    warehouse.execute_script(sql_path("030_core.sql"))
    return {
        "fact_rows": int(warehouse.scalar("SELECT COUNT(*) FROM core.fct_theta_cycle") or 0),
        "channels": int(warehouse.scalar("SELECT COUNT(*) FROM core.dim_channel") or 0),
        "trials": int(warehouse.scalar("SELECT COUNT(*) FROM core.dim_trial") or 0),
    }


def build_marts(warehouse: Warehouse) -> dict[str, int]:
    warehouse.execute_script(sql_path("040_marts.sql"))
    return {
        "channel_load_rows": int(
            warehouse.scalar("SELECT COUNT(*) FROM mart.channel_load_asym") or 0
        ),
        "paired_channels": int(
            warehouse.scalar("SELECT COUNT(*) FROM mart.channel_paired_asym") or 0
        ),
        "included_channels": int(
            warehouse.scalar("SELECT COUNT(*) FROM mart.channel_paired_asym WHERE included") or 0
        ),
    }


def fetch_paired_values(warehouse: Warehouse, metric: str) -> tuple[list[float], list[float]]:
    """Read the paired per-channel values for one metric.

    Ordering by channel keeps the pairing stable across calls, which matters
    because the permutation null is seeded: an unordered read would make results
    non-reproducible across calls.
    """
    prefix = {"time_ptsym": "ptsym", "time_rdsym": "rdsym"}.get(metric)
    if prefix is None:
        raise ValueError(f"unsupported metric for the paired mart: {metric!r}")

    rows = warehouse.rows(
        f"""
        SELECT {prefix}_baseline, {prefix}_comparison
        FROM mart.channel_paired_asym
        WHERE included
        ORDER BY subject_id, region, channel_label
        """
    )
    baseline = [float(row[0]) for row in rows]
    comparison = [float(row[1]) for row in rows]
    return baseline, comparison


def run_analysis(warehouse: Warehouse, config: Config, run_id: str) -> list[TestResult]:
    """Run the paired and one-sample tests and persist the results.

    Two questions, kept separate on purpose:

    * Does waveform asymmetry differ between conditions? (paired test)
    * Is the waveform symmetric at all in each condition? (one-sample vs 0.5)

    A null on the first with symmetric waveforms on the second is the pattern
    that rules waveform shape out as an explanation for a coupling difference.
    Only the paired family is multiplicity-corrected, since that is the family
    the conclusion rests on.
    """
    paired_results: list[TestResult] = []
    descriptive_results: list[TestResult] = []

    for metric in config.analysis.metrics:
        baseline, comparison = fetch_paired_values(warehouse, metric)
        if len(baseline) < 2:
            raise RuntimeError(
                f"only {len(baseline)} included channel(s) for {metric}; "
                "cannot run a paired test. Inspect mart.channel_paired_asym."
            )

        paired_results.append(
            paired_permutation_test(
                baseline,
                comparison,
                metric=metric,
                n_permutations=config.analysis.n_permutations,
                statistic="t_stat",
                seed=config.analysis.random_seed,
            )
        )

        # Symmetry check per condition, pooling channels within condition.
        for label, values in (
            (f"load{config.analysis.baseline_condition}", baseline),
            (f"load{config.analysis.comparison_condition}", comparison),
        ):
            descriptive_results.append(
                one_sample_permutation_test(
                    values,
                    metric=f"{metric}@{label}",
                    null_value=config.analysis.symmetry_null_value,
                    n_permutations=config.analysis.n_permutations,
                    statistic="t_stat",
                    seed=config.analysis.random_seed,
                )
            )

    corrected = holm_bonferroni(paired_results)
    all_results = corrected + descriptive_results

    computed_at = datetime.now(timezone.utc)
    warehouse.execute("DELETE FROM ops.test_result WHERE run_id = ?", [run_id])
    for result in all_results:
        row = result.as_row()
        warehouse.execute(
            """
            INSERT INTO ops.test_result
                (run_id, metric, test, statistic, n_units, observed, p_value,
                 p_value_adjusted, adjustment, effect_size_dz, ci_lower, ci_upper,
                 ci_level, mean_baseline, mean_comparison, n_permutations, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                row["metric"],
                row["test"],
                row["statistic"],
                row["n_units"],
                row["observed"],
                row["p_value"],
                row["p_value_adjusted"],
                row["adjustment"],
                row["effect_size_dz"],
                row["ci_lower"],
                row["ci_upper"],
                row["ci_level"],
                row["mean_baseline"],
                row["mean_comparison"],
                row["n_permutations"],
                computed_at,
            ],
        )

    return all_results


def format_results(results: list[TestResult]) -> str:
    """Render test results for CLI output."""
    if not results:
        return "no tests run"
    lines = []
    for result in results:
        p_value = (
            f"p={result.p_value:.4f}"
            if result.p_value_adjusted is None
            else f"p={result.p_value:.4f} (Holm {result.p_value_adjusted:.4f})"
        )
        lines.append(
            f"  {result.metric:<24} {result.test:<22} n={result.n_units:<4} "
            f"stat={result.observed:+.4f} {p_value} "
            f"dz={result.effect_size_dz:+.3f} "
            f"CI[{result.ci_lower:+.4f}, {result.ci_upper:+.4f}]"
        )
    return "\n".join(lines)
