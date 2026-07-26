"""Tests for dashboard data-processing helpers (no file I/O needed)."""
from dashboard.build_dashboard import (
    EXTRACTS,
    build_difference_values,
    build_paired_points,
    compute_headline,
    summarise_coverage,
    to_bool,
    to_float,
)


def test_to_float_parses_numeric():
    assert to_float("0.5") == 0.5
    assert to_float("  1.23  ") == 1.23


def test_to_float_returns_none_on_blank():
    assert to_float("") is None
    assert to_float("nan") is None
    assert to_float(None) is None


def test_to_bool_parses_true_spellings():
    for v in ("true", "True", "TRUE", "t", "1"):
        assert to_bool(v) is True


def test_to_bool_parses_false_spellings():
    for v in ("false", "False", "0", None, ""):
        assert to_bool(v) is False


def test_build_paired_points_filters_excluded():
    rows = [
        {"included": "True", "ptsym_baseline": "0.48", "ptsym_comparison": "0.52", "subject_id": "S1", "channel_label": "HPC1"},
        {"included": "False", "ptsym_baseline": "0.44", "ptsym_comparison": "0.56", "subject_id": "S1", "channel_label": "HPC2"},
    ]
    points = build_paired_points(rows, "ptsym")
    assert len(points) == 1
    assert points[0]["baseline"] == 0.48


def test_build_difference_values_sums_included_only():
    rows = [
        {"included": "True", "ptsym_diff": "0.04"},
        {"included": "False", "ptsym_diff": "0.10"},
    ]
    diffs = build_difference_values(rows, "ptsym")
    assert diffs == [0.04]


def test_summarise_coverage_aggregates_by_subject():
    rows = [
        {"subject_id": "S1", "n_channels": "10", "n_channels_included": "8", "n_cycles": "500"},
        {"subject_id": "S1", "n_channels": "5", "n_channels_included": "4", "n_cycles": "200"},
    ]
    summary = summarise_coverage(rows)
    assert len(summary) == 1
    assert summary[0]["n_channels"] == 15
    assert summary[0]["n_channels_included"] == 12


def test_extracts_dict_has_seven_entries():
    assert len(EXTRACTS) == 7


def test_compute_headline_counts_subjects_and_channels():
    coverage = [
        {"subject_id": "S1", "n_channels": "10", "n_channels_included": "8", "n_cycles": "500"},
        {"subject_id": "S2", "n_channels": "12", "n_channels_included": "10", "n_cycles": "600"},
    ]
    paired = [{"included": "True"}, {"included": "False"}]
    headline = compute_headline({}, coverage, paired, [], [])
    assert headline["n_subjects"] == 2
    assert headline["n_channels"] == 22
    assert headline["n_channels_included"] == 1
