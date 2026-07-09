"""Tests for transform.py pure-logic helpers."""
import pytest

from theta_warehouse.stats import TestResult
from theta_warehouse.transform import fetch_paired_values, format_results


def _make_result(**kwargs) -> TestResult:
    defaults = dict(
        metric="time_ptsym",
        test="paired_permutation",
        statistic="t_stat",
        n_units=20,
        observed=1.5,
        p_value=0.04,
        p_value_adjusted=None,
        adjustment=None,
        effect_size_dz=0.35,
        ci_lower=-0.01,
        ci_upper=0.08,
        ci_level=0.95,
        mean_baseline=0.48,
        mean_comparison=0.52,
        n_permutations=10000,
    )
    defaults.update(kwargs)
    return TestResult(**defaults)


def test_format_results_empty():
    assert format_results([]) == "no tests run"


def test_format_results_includes_metric_name():
    result = _make_result(metric="time_rdsym")
    output = format_results([result])
    assert "time_rdsym" in output


def test_format_results_shows_holm_when_adjusted():
    result = _make_result(p_value=0.04, p_value_adjusted=0.08, adjustment="holm")
    output = format_results([result])
    assert "Holm" in output


def test_fetch_paired_values_raises_on_unknown_metric():
    warehouse = type("W", (), {"rows": lambda self, sql: []})()
    with pytest.raises(ValueError, match="unsupported metric"):
        fetch_paired_values(warehouse, "unknown_metric")
