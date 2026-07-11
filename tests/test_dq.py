"""Tests for the data-quality check logic."""
from unittest.mock import MagicMock

import pytest

from theta_warehouse.dq import (
    Check,
    CheckOutcome,
    DataQualityError,
    build_checks,
    format_outcomes,
    run_checks,
)


def _make_config() -> MagicMock:
    cfg = MagicMock()
    cfg.signal.fs = 400
    cfg.signal.f_theta_low = 3.0
    cfg.signal.f_theta_high = 7.0
    cfg.dq.symmetry_lower_bound = 0.0
    cfg.dq.symmetry_upper_bound = 1.0
    cfg.dq.max_null_fraction = 0.05
    cfg.dq.max_channel_dropout_fraction = 0.10
    return cfg


def _make_check(name="chk", severity="error", comparison=">=", threshold=1.0) -> Check:
    return Check(
        name=name,
        severity=severity,
        sql="SELECT 1",
        comparison=comparison,
        threshold=threshold,
        detail="test detail",
    )


def test_check_passes_when_comparison_holds():
    check = _make_check(comparison=">=", threshold=5.0)
    assert check.evaluate(5.0) is True
    assert check.evaluate(4.9) is False


def test_check_fails_on_none_observed():
    check = _make_check()
    assert check.evaluate(None) is False


def test_check_outcome_is_blocking_only_for_error_failures():
    check_err = _make_check(severity="error")
    check_warn = _make_check(severity="warn")
    assert CheckOutcome(check=check_err, observed=0.0, passed=False).is_blocking is True
    assert CheckOutcome(check=check_warn, observed=0.0, passed=False).is_blocking is False
    assert CheckOutcome(check=check_err, observed=5.0, passed=True).is_blocking is False


def test_build_checks_returns_twelve_checks():
    cfg = _make_config()
    checks = build_checks(cfg)
    assert len(checks) == 12


def test_format_outcomes_empty():
    assert format_outcomes([]) == "no checks run"


def test_format_outcomes_shows_pass_and_fail():
    check = _make_check(name="mycheck", severity="error")
    outcomes = [
        CheckOutcome(check=check, observed=5.0, passed=True),
        CheckOutcome(check=check, observed=0.0, passed=False),
    ]
    output = format_outcomes(outcomes)
    assert "PASS" in output
    assert "ERROR" in output


def test_run_checks_raises_on_blocking_failure():
    cfg = _make_config()
    warehouse = MagicMock()
    warehouse.scalar.return_value = 0.0

    blocking_check = _make_check(name="fact_not_empty", severity="error", comparison=">", threshold=0.0)
    warehouse.execute.return_value = None

    with pytest.raises(DataQualityError):
        from theta_warehouse.dq import run_checks
        import theta_warehouse.dq as dq_mod
        original = dq_mod.build_checks
        dq_mod.build_checks = lambda cfg: [blocking_check]
        try:
            run_checks(warehouse, cfg, "run_001", raise_on_error=True)
        finally:
            dq_mod.build_checks = original
