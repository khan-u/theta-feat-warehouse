"""Tests for export module pure-logic (EXTRACTS contract, manifest shape)."""
import json
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from theta_warehouse.export import EXTRACTS, export_extracts


def test_extracts_tuple_has_seven_entries():
    assert len(EXTRACTS) == 7


def test_extracts_names_are_unique():
    names = [e[0] for e in EXTRACTS]
    assert len(names) == len(set(names))


def test_extracts_tuple_is_ordered_mart_first():
    mart_entries = [e for e in EXTRACTS if e[1].startswith("mart.")]
    ops_entries = [e for e in EXTRACTS if e[1].startswith("ops.")]
    mart_idxs = [EXTRACTS.index(e) for e in mart_entries]
    ops_idxs = [EXTRACTS.index(e) for e in ops_entries]
    assert max(mart_idxs) < min(ops_idxs)


def test_export_extracts_writes_manifest(tmp_path):
    warehouse = MagicMock()
    warehouse.scalar.return_value = 10

    cfg = MagicMock()
    cfg.paths.export_dir = tmp_path
    cfg.paths.duckdb_path = tmp_path / "theta.duckdb"
    cfg.analysis.burst_only = True
    cfg.analysis.baseline_condition = 1
    cfg.analysis.comparison_condition = 3
    cfg.analysis.min_cycles_per_channel_load = 10
    cfg.analysis.n_permutations = 10000
    cfg.analysis.random_seed = 42
    cfg.signal.fs = 400
    cfg.signal.f_theta_low = 3.0
    cfg.signal.f_theta_high = 7.0
    cfg.signal.f_lowpass = 30.0
    cfg.signal.epoch_start_s = -0.3
    cfg.signal.epoch_end_s = 2.8

    export_extracts(warehouse, cfg, "run_001")

    manifest_path = tmp_path / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["run_id"] == "run_001"
    assert "row_counts" in manifest
    assert len(manifest["row_counts"]) == len(EXTRACTS)
