import textwrap
from pathlib import Path

import pytest

from theta_warehouse.config import load_config


def _write_config(tmp_path: Path, overrides: dict[str, str]) -> Path:
    base = textwrap.dedent("""\
        paths:
          source_root: data/cycle_features
          trial_metadata: data/trial_metadata
          warehouse_dir: warehouse
          parquet_root: warehouse/lake
          duckdb_path: warehouse/theta.duckdb
          export_dir: warehouse/exports
        signal:
          fs: 400
          f_theta: [3, 7]
          f_lowpass: 30
          epoch_window_s: [-0.3, 2.8]
        bycycle_thresholds:
          amp_fraction_threshold: 0.2
          amp_consistency_threshold: 0.1
          period_consistency_threshold: 0.4
          monotonicity_threshold: 0.4
          min_n_cycles: 3
        analysis:
          burst_only: true
          metrics: [time_ptsym, time_rdsym]
          conditions:
            baseline: 1
            comparison: 3
          min_cycles_per_channel_load: 10
          n_permutations: 10000
          random_seed: 42
          symmetry_null_value: 0.5
        dq:
          max_null_fraction: 0.05
          max_channel_dropout_fraction: 0.10
          symmetry_bounds: [0.0, 1.0]
          min_rows_per_file: 1
    """)
    for key, value in overrides.items():
        base = base.replace(key, value)
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "pipeline.yml"
    cfg_file.write_text(base)
    return cfg_file


def test_invalid_theta_band_raises(tmp_path):
    cfg = _write_config(tmp_path, {"f_theta: [3, 7]": "f_theta: [7, 3]"})
    with pytest.raises(ValueError, match="invalid theta band"):
        load_config(cfg)


def test_low_n_permutations_raises(tmp_path):
    cfg = _write_config(tmp_path, {"n_permutations: 10000": "n_permutations: 500"})
    with pytest.raises(ValueError, match="n_permutations"):
        load_config(cfg)
