"""This module tests that the synthetic generator reproduces the real file contract.

If the generator drifts from what RunBycycle.py writes, the pipeline could pass
its own tests while failing on real data. These tests therefore assert on the
byte-level details: BOM, column order, NULL encoding, boolean spelling.
"""

from __future__ import annotations

import csv
from pathlib import Path

import numpy as np
import pytest

from theta_warehouse.naming import parse_source_path
from theta_warehouse.schema import SOURCE_COLUMN_ORDER
from theta_warehouse.synth import SynthSpec, generate, spec_from_profile


@pytest.fixture
def generated(tmp_path: Path):
    spec = SynthSpec(n_subjects=2, n_channels_total=4, n_trials=6, seed=99)
    totals = generate(
        spec,
        source_root=tmp_path / "cycle_features",
        trial_metadata_root=tmp_path / "trial_metadata",
    )
    return spec, tmp_path, totals


def test_writes_one_file_per_subject_region(generated):
    spec, root, totals = generated
    files = list((root / "cycle_features").rglob("*.csv"))
    assert len(files) == spec.n_subjects * len(spec.regions)
    assert totals["files"] == len(files)
    assert totals["rows"] > 0


def test_directory_layout_matches_the_naming_contract(generated):
    _, root, _ = generated
    source_root = root / "cycle_features"
    for path in source_root.rglob("*.csv"):
        source = parse_source_path(path, source_root)
        assert source.region == "HP"


def test_file_carries_a_utf8_bom(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_header_matches_bycycle_column_order(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        header = next(csv.reader(handle))
    assert tuple(header) == SOURCE_COLUMN_ORDER


def test_consistency_columns_are_null_at_signal_edges(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    # The first row of the file is the first cycle of a signal, where bycycle
    # cannot compute a consistency criterion.
    assert rows[0]["amp_consistency"] == ""
    assert rows[0]["period_consistency"] == ""
    # And a NULL criterion must never be labelled part of a burst.
    assert rows[0]["is_burst"] == "False"


def test_booleans_are_written_the_way_pandas_writes_them(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        values = {row["is_burst"] for row in csv.DictReader(handle)}
    assert values <= {"True", "False"}


def test_symmetry_metrics_stay_inside_the_bycycle_range(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    for column in ("time_ptsym", "time_rdsym"):
        values = np.array([float(row[column]) for row in rows])
        assert values.min() > 0.0
        assert values.max() < 1.0
        # Null generation should centre on symmetry.
        assert values.mean() == pytest.approx(0.5, abs=0.05)


def test_trial_metadata_covers_every_trial(generated):
    spec, root, _ = generated
    files = list((root / "trial_metadata").glob("*_trial_metadata.csv"))
    assert len(files) == spec.n_subjects

    with files[0].open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == spec.n_trials
    # 0-based, to join against the Python trial index in the feature CSVs.
    assert {int(row["trial"]) for row in rows} == set(range(spec.n_trials))
    assert {int(row["load_condition"]) for row in rows} == set(spec.loads)


def test_burst_runs_respect_min_n_cycles(generated):
    _, root, _ = generated
    path = next((root / "cycle_features").rglob("*.csv"))
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    # Walk one trial x channel block and measure contiguous burst run lengths.
    block = [r for r in rows if r["trial"] == "0" and r["channel_idx"] == "0"]
    runs, current = [], 0
    for row in block:
        if row["is_burst"] == "True":
            current += 1
        elif current:
            runs.append(current)
            current = 0
    if current:
        runs.append(current)
    assert all(run >= 3 for run in runs), runs


def test_effect_injection_shifts_the_comparison_condition(tmp_path: Path):
    spec = SynthSpec(n_subjects=1, n_channels_total=2, n_trials=8, effect=0.05, seed=3)
    generate(
        spec,
        source_root=tmp_path / "cycle_features",
        trial_metadata_root=tmp_path / "trial_metadata",
    )

    metadata_path = next((tmp_path / "trial_metadata").glob("*.csv"))
    with metadata_path.open("r", encoding="utf-8", newline="") as handle:
        loads = {int(r["trial"]): int(r["load_condition"]) for r in csv.DictReader(handle)}

    features_path = next((tmp_path / "cycle_features").rglob("*.csv"))
    with features_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    by_load: dict[int, list[float]] = {1: [], 3: []}
    for row in rows:
        by_load[loads[int(row["trial"])]].append(float(row["time_ptsym"]))

    assert np.mean(by_load[3]) > np.mean(by_load[1])


def test_full_profile_matches_the_reference_scale():
    spec = spec_from_profile("full")
    assert spec.n_subjects == 32
    assert spec.n_channels_total == 586
