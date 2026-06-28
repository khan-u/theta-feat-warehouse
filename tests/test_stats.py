"""This module tests the permutation tests.

The important properties are calibration (a null-true dataset should not produce
small p-values) and power (a real shift should be detected). Both are checked
against seeded data, and the paired test is cross-checked against scipy's
parametric paired t-test, which should agree closely on Gaussian differences.
"""

from __future__ import annotations

import numpy as np
import pytest
from scipy import stats as scipy_stats

from theta_warehouse.stats import (
    holm_bonferroni,
    one_sample_permutation_test,
    paired_permutation_test,
)


def test_null_is_calibrated():
    """False-positive rate should sit near alpha across null-true datasets.

    Asserting that one particular null dataset yields a large p-value would be a
    flaky test, since p is uniform under the null and any single draw can land
    below 0.05 by definition. Calibration across replicates is the property that
    actually has to hold.
    """
    rng = np.random.default_rng(1)
    n_replicates = 200
    rejections = 0

    for _ in range(n_replicates):
        baseline = rng.normal(0.5, 0.03, size=80)
        comparison = baseline + rng.normal(0.0, 0.01, size=80)
        result = paired_permutation_test(
            baseline,
            comparison,
            metric="time_ptsym",
            n_permutations=500,
            n_bootstrap=100,
            seed=int(rng.integers(0, 1_000_000)),
        )
        rejections += result.p_value < 0.05

    rate = rejections / n_replicates
    # Binomial 99% interval around 0.05 for n=200 is roughly [0.01, 0.11].
    assert 0.01 <= rate <= 0.11, f"false-positive rate {rate:.3f} is not near 0.05"


def test_reports_shape_of_the_result():
    rng = np.random.default_rng(11)
    baseline = rng.normal(0.5, 0.03, size=137)
    comparison = baseline + rng.normal(0.0, 0.01, size=137)

    result = paired_permutation_test(
        baseline, comparison, metric="time_ptsym", n_permutations=2000, seed=7
    )
    assert result.n_units == 137
    assert result.ci_lower < result.ci_upper
    assert 0.0 < result.p_value <= 1.0
    assert result.mean_baseline == pytest.approx(baseline.mean())
    assert result.mean_comparison == pytest.approx(comparison.mean())


def test_real_shift_is_detected():
    rng = np.random.default_rng(2)
    baseline = rng.normal(0.5, 0.03, size=137)
    comparison = baseline + 0.02 + rng.normal(0.0, 0.01, size=137)

    result = paired_permutation_test(
        baseline, comparison, metric="time_ptsym", n_permutations=4000, seed=7
    )
    assert result.p_value < 0.01
    assert result.effect_size_dz > 0.5
    assert result.ci_lower > 0.0


def test_agrees_with_parametric_paired_t_test():
    rng = np.random.default_rng(3)
    baseline = rng.normal(0.5, 0.04, size=90)
    comparison = baseline + rng.normal(0.004, 0.02, size=90)

    permutation = paired_permutation_test(
        baseline,
        comparison,
        metric="time_rdsym",
        n_permutations=20000,
        statistic="t_stat",
        seed=11,
    )
    parametric = scipy_stats.ttest_rel(comparison, baseline)

    assert permutation.observed == pytest.approx(parametric.statistic, rel=1e-9)
    # Sign-flip and parametric p-values agree closely for Gaussian differences.
    assert permutation.p_value == pytest.approx(parametric.pvalue, abs=0.02)


def test_p_value_is_never_zero():
    rng = np.random.default_rng(4)
    baseline = rng.normal(0.5, 0.01, size=50)
    comparison = baseline + 1.0  # enormous, unambiguous effect

    result = paired_permutation_test(
        baseline, comparison, metric="time_ptsym", n_permutations=1000, seed=5
    )
    # The add-one correction floors the p-value at 1/(1+n) rather than 0.
    assert result.p_value == pytest.approx(1.0 / 1001.0)


def test_results_are_reproducible_under_a_fixed_seed():
    rng = np.random.default_rng(5)
    baseline = rng.normal(0.5, 0.03, size=60)
    comparison = baseline + rng.normal(0.002, 0.02, size=60)

    kwargs = dict(metric="time_ptsym", n_permutations=3000, seed=42)
    first = paired_permutation_test(baseline, comparison, **kwargs)
    second = paired_permutation_test(baseline, comparison, **kwargs)
    assert first.p_value == second.p_value
    assert first.ci_lower == second.ci_lower


def test_one_sample_test_detects_asymmetry():
    rng = np.random.default_rng(6)
    symmetric = rng.normal(0.5, 0.02, size=100)
    skewed = rng.normal(0.56, 0.02, size=100)

    assert (
        one_sample_permutation_test(
            symmetric, metric="time_ptsym", n_permutations=4000, seed=1
        ).p_value
        > 0.10
    )
    assert (
        one_sample_permutation_test(
            skewed, metric="time_ptsym", n_permutations=4000, seed=1
        ).p_value
        < 0.01
    )


def test_unequal_pair_counts_are_rejected():
    with pytest.raises(ValueError, match="unequal pair counts"):
        paired_permutation_test([0.5, 0.5, 0.5], [0.5, 0.5], metric="time_ptsym")


def test_non_finite_input_is_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        paired_permutation_test([0.5, np.nan], [0.5, 0.5], metric="time_ptsym")


def test_holm_correction_is_monotone_and_bounded():
    rng = np.random.default_rng(7)
    results = []
    for shift, metric in ((0.0, "time_ptsym"), (0.03, "time_rdsym")):
        baseline = rng.normal(0.5, 0.03, size=80)
        comparison = baseline + shift + rng.normal(0, 0.01, size=80)
        results.append(
            paired_permutation_test(
                baseline, comparison, metric=metric, n_permutations=2000, seed=3
            )
        )

    corrected = holm_bonferroni(results)
    assert all(c.adjustment == "holm-bonferroni" for c in corrected)
    for original, adjusted in zip(results, corrected):
        assert adjusted.p_value_adjusted >= original.p_value
        assert adjusted.p_value_adjusted <= 1.0
