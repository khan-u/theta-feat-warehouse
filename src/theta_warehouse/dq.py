"""This module is the data-quality gate.

Checks run against the core fact table after it is built and before any mart or
statistical result is published. Each check is a scalar SQL query, a comparison
and a threshold; results are written to ``ops.dq_result`` so quality is visible
in the dashboard over time rather than only in a log line.

Severity matters: ``error`` fails the run and stops the DAG before marts are
rebuilt, ``warn`` records the observation and continues. The split reflects
whether a downstream number would be wrong or only worth checking.
"""

from __future__ import annotations

import operator
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from .config import Config
from .db import Warehouse

COMPARISONS: dict[str, Callable[[float, float], bool]] = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
}


class DataQualityError(RuntimeError):
    """Raised when one or more error-severity checks fail."""


@dataclass(frozen=True)
class Check:
    name: str
    severity: str  # error | warn
    sql: str
    comparison: str
    threshold: float
    detail: str

    def evaluate(self, observed: float | None) -> bool:
        if observed is None:
            return False
        return COMPARISONS[self.comparison](float(observed), self.threshold)


@dataclass(frozen=True)
class CheckOutcome:
    check: Check
    observed: float | None
    passed: bool

    @property
    def is_blocking(self) -> bool:
        return not self.passed and self.check.severity == "error"


def build_checks(config: Config) -> list[Check]:
    """Assemble the check suite from config thresholds."""
    fs = config.signal.fs
    theta_low = config.signal.f_theta_low
    theta_high = config.signal.f_theta_high
    lower = config.dq.symmetry_lower_bound
    upper = config.dq.symmetry_upper_bound

    return [
        Check(
            name="fact_not_empty",
            severity="error",
            sql="SELECT COUNT(*) FROM core.fct_theta_cycle",
            comparison=">",
            threshold=0.0,
            detail="Fact table has no rows; ingestion produced nothing usable.",
        ),
        Check(
            name="cycle_grain_unique",
            severity="error",
            sql="""
                SELECT COUNT(*) FROM (
                    SELECT subject_id, region, channel_label, trial, cycle_idx
                    FROM core.fct_theta_cycle
                    GROUP BY subject_id, region, channel_label, trial, cycle_idx
                    HAVING COUNT(*) > 1
                )
            """,
            comparison="==",
            threshold=0.0,
            detail=(
                "Duplicate rows at cycle grain. Most likely a merged session CSV "
                "was ingested alongside its region files, or two extractions were "
                "not de-duplicated."
            ),
        ),
        Check(
            name="single_extraction_per_partition",
            severity="error",
            sql="""
                SELECT COUNT(*) FROM (
                    SELECT subject_id, region
                    FROM core.fct_theta_cycle
                    GROUP BY subject_id, region
                    HAVING COUNT(DISTINCT extraction_id) > 1
                )
            """,
            comparison="==",
            threshold=0.0,
            detail="More than one extraction timestamp survived into the fact table.",
        ),
        Check(
            name="symmetry_null_fraction",
            severity="error",
            sql="""
                SELECT COALESCE(
                    SUM(CASE WHEN time_ptsym IS NULL OR time_rdsym IS NULL THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(COUNT(*), 0), 0.0)
                FROM core.fct_theta_cycle
            """,
            comparison="<=",
            threshold=config.dq.max_null_fraction,
            detail=(
                "Too many cycles are missing a symmetry measure. Unlike "
                "amp_consistency, these are not undefined at signal edges, so "
                "widespread NULLs point at a truncated or corrupt source file."
            ),
        ),
        Check(
            name="symmetry_within_bounds",
            severity="error",
            sql=f"""
                SELECT COUNT(*)
                FROM core.fct_theta_cycle
                WHERE (time_ptsym IS NOT NULL AND (time_ptsym < {lower} OR time_ptsym > {upper}))
                   OR (time_rdsym IS NOT NULL AND (time_rdsym < {lower} OR time_rdsym > {upper}))
            """,
            comparison="==",
            threshold=0.0,
            detail=(
                f"bycycle defines both symmetry measures on [{lower}, {upper}]. "
                "Values outside that range mean the column mapping is wrong."
            ),
        ),
        Check(
            name="trial_condition_coverage",
            severity="error",
            sql="""
                SELECT COALESCE(
                    SUM(CASE WHEN load_condition IS NULL THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(COUNT(*), 0), 1.0)
                FROM core.fct_theta_cycle
            """,
            comparison="<=",
            threshold=config.dq.max_null_fraction,
            detail=(
                "Cycles could not be matched to a load condition. Check that "
                "trial metadata was exported for every subject and that trial "
                "numbering matches the feature files (both are 0-based)."
            ),
        ),
        Check(
            name="channel_dropout",
            severity="error",
            sql="""
                SELECT COALESCE(
                    COUNT(*) FILTER (WHERE NOT included) * 1.0 / NULLIF(COUNT(*), 0), 1.0)
                FROM mart.channel_paired_asym
            """,
            comparison="<=",
            threshold=config.dq.max_channel_dropout_fraction,
            detail=(
                "Too large a share of channels failed the paired-inclusion rule. "
                "See exclusion_reason in mart.channel_paired_asym."
            ),
        ),
        Check(
            name="paired_units_sufficient",
            severity="error",
            sql="SELECT COUNT(*) FROM mart.channel_paired_asym WHERE included",
            comparison=">=",
            threshold=2.0,
            detail="A paired permutation test needs at least two included channels.",
        ),
        Check(
            name="cycle_frequency_in_band",
            severity="warn",
            sql=f"""
                SELECT COALESCE(
                    SUM(CASE WHEN cycle_freq_hz < {theta_low * 0.6}
                              OR cycle_freq_hz > {theta_high * 1.6}
                             THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(COUNT(*), 0), 0.0)
                FROM core.fct_theta_cycle
                WHERE cycle_freq_hz IS NOT NULL
            """,
            comparison="<=",
            threshold=0.20,
            detail=(
                f"Many detected cycles fall well outside the {theta_low}-{theta_high} Hz "
                f"band the features were extracted in (fs = {fs} Hz). Suggests a "
                "sampling-rate or band mismatch between config and extraction."
            ),
        ),
        Check(
            name="burst_fraction_plausible",
            severity="warn",
            sql="""
                SELECT COALESCE(
                    COUNT(*) FILTER (WHERE is_burst) * 1.0 / NULLIF(COUNT(*), 0), 0.0)
                FROM core.fct_theta_cycle
            """,
            comparison=">=",
            threshold=0.05,
            detail=(
                "Almost no cycles were labelled as part of a burst. The analysis "
                "filters to burst cycles by default, so this would leave it with "
                "little data. Check the bycycle thresholds."
            ),
        ),
        Check(
            name="trial_coverage_gaps",
            severity="error",
            sql="""
                SELECT COALESCE(
                    SUM(CASE WHEN f.n_cycles IS NULL OR f.n_cycles = 0 THEN 1 ELSE 0 END)
                    * 1.0 / NULLIF(COUNT(*), 0), 0.0)
                FROM core.trial_metadata AS t
                LEFT JOIN (
                    SELECT subject_id, trial, COUNT(*) AS n_cycles
                    FROM core.fct_theta_cycle
                    GROUP BY subject_id, trial
                ) AS f
                  ON t.subject_id = f.subject_id AND t.trial = f.trial
            """,
            comparison="<=",
            threshold=config.dq.max_null_fraction,
            detail=(
                "Too large a share of trials present in the metadata have no "
                "cycles in the fact table, i.e. gaps in trial coverage. This "
                "points at a truncated recording, a dropped channel set, or a "
                "failed extraction for those trials."
            ),
        ),
        Check(
            name="no_orphan_trials",
            severity="warn",
            sql="""
                SELECT COUNT(*)
                FROM core.trial_metadata AS t
                LEFT JOIN (
                    SELECT DISTINCT subject_id, trial FROM core.fct_theta_cycle
                ) AS f
                  ON t.subject_id = f.subject_id AND t.trial = f.trial
                WHERE f.trial IS NULL
            """,
            comparison="==",
            threshold=0.0,
            detail=(
                "Trials exist in the metadata with no cycles in the features. "
                "Expected if trials were rejected upstream for NaNs, worth "
                "confirming the count matches the extraction log."
            ),
        ),
    ]


def run_checks(
    warehouse: Warehouse,
    config: Config,
    run_id: str,
    raise_on_error: bool = True,
) -> list[CheckOutcome]:
    """Execute the suite, persist outcomes, and optionally fail the run."""
    checked_at = datetime.now(timezone.utc)
    outcomes: list[CheckOutcome] = []

    for check in build_checks(config):
        try:
            observed = warehouse.scalar(check.sql)
        except Exception as exc:
            observed = None
            detail = f"{check.detail} (check query failed: {exc})"
        else:
            detail = check.detail

        observed_value = None if observed is None else float(observed)
        passed = check.evaluate(observed_value)
        outcomes.append(CheckOutcome(check=check, observed=observed_value, passed=passed))

        warehouse.execute(
            """
            INSERT INTO ops.dq_result
                (run_id, check_name, severity, passed, observed, threshold, detail, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run_id,
                check.name,
                check.severity,
                passed,
                observed_value,
                check.threshold,
                detail,
                checked_at,
            ],
        )

    blocking = [o for o in outcomes if o.is_blocking]
    if blocking and raise_on_error:
        lines = [
            f"  - {o.check.name}: observed={o.observed} "
            f"{o.check.comparison} {o.check.threshold} failed. {o.check.detail}"
            for o in blocking
        ]
        raise DataQualityError(
            f"{len(blocking)} data-quality check(s) failed:\n" + "\n".join(lines)
        )

    return outcomes


def format_outcomes(outcomes: list[CheckOutcome]) -> str:
    """Render outcomes as an aligned text block for logs and CLI output."""
    if not outcomes:
        return "no checks run"
    width = max(len(o.check.name) for o in outcomes)
    lines = []
    for outcome in outcomes:
        status = "PASS" if outcome.passed else outcome.check.severity.upper()
        observed = "n/a" if outcome.observed is None else f"{outcome.observed:.6g}"
        lines.append(
            f"  [{status:<5}] {outcome.check.name:<{width}}  "
            f"observed={observed:<12} {outcome.check.comparison} {outcome.check.threshold:g}"
        )
    return "\n".join(lines)
