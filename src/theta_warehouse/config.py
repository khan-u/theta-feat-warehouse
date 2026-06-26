"""This module provides typed access to config/pipeline.yml.

Signal and threshold values are duplicated here from eeg-feat-ext so that the
warehouse can assert that the features it is aggregating were produced under the
parameters the analysis assumes (fs = 400 Hz, theta = 3-7 Hz, and the five
bycycle burst thresholds set in RunBycycle.py). If the upstream script is
re-run with different settings, the config is the one place to change and the
recorded run metadata makes the change visible in the dashboard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path("config/pipeline.yml")


@dataclass(frozen=True)
class Paths:
    source_root: Path
    trial_metadata: Path
    warehouse_dir: Path
    parquet_root: Path
    duckdb_path: Path
    export_dir: Path

    def ensure(self) -> None:
        for directory in (self.warehouse_dir, self.parquet_root, self.export_dir):
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Signal:
    fs: int
    f_theta_low: float
    f_theta_high: float
    f_lowpass: float
    epoch_start_s: float
    epoch_end_s: float


@dataclass(frozen=True)
class BycycleThresholds:
    amp_fraction_threshold: float
    amp_consistency_threshold: float
    period_consistency_threshold: float
    monotonicity_threshold: float
    min_n_cycles: int


@dataclass(frozen=True)
class Analysis:
    burst_only: bool
    metrics: tuple[str, ...]
    baseline_condition: int
    comparison_condition: int
    min_cycles_per_channel_load: int
    n_permutations: int
    random_seed: int
    symmetry_null_value: float


@dataclass(frozen=True)
class DataQuality:
    max_null_fraction: float
    max_channel_dropout_fraction: float
    symmetry_lower_bound: float
    symmetry_upper_bound: float
    min_rows_per_file: int


@dataclass(frozen=True)
class Config:
    paths: Paths
    signal: Signal
    thresholds: BycycleThresholds
    analysis: Analysis
    dq: DataQuality

    @property
    def sql_context(self) -> dict[str, str]:
        """Values substituted into the .sql files as ``{{name}}`` placeholders."""
        return {
            "parquet_root": self.paths.parquet_root.as_posix(),
            "export_dir": self.paths.export_dir.as_posix(),
            "fs": str(self.signal.fs),
            "burst_only": "TRUE" if self.analysis.burst_only else "FALSE",
            "baseline_condition": str(self.analysis.baseline_condition),
            "comparison_condition": str(self.analysis.comparison_condition),
            "min_cycles_per_channel_load": str(self.analysis.min_cycles_per_channel_load),
            "symmetry_null_value": str(self.analysis.symmetry_null_value),
            "symmetry_lower_bound": str(self.dq.symmetry_lower_bound),
            "symmetry_upper_bound": str(self.dq.symmetry_upper_bound),
        }


def _resolve(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate pipeline configuration.

    Paths in the file are interpreted relative to the *project root*, taken as
    the parent of the config file's directory, so the pipeline behaves the same
    whether invoked from the repo root or from an Airflow worker's cwd.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    config_path = config_path.expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}

    project_root = config_path.parent.parent

    paths_cfg = data.get("paths", {})
    paths = Paths(
        source_root=_resolve(project_root, paths_cfg["source_root"]),
        trial_metadata=_resolve(project_root, paths_cfg["trial_metadata"]),
        warehouse_dir=_resolve(project_root, paths_cfg["warehouse_dir"]),
        parquet_root=_resolve(project_root, paths_cfg["parquet_root"]),
        duckdb_path=_resolve(project_root, paths_cfg["duckdb_path"]),
        export_dir=_resolve(project_root, paths_cfg["export_dir"]),
    )

    signal_cfg = data.get("signal", {})
    theta_low, theta_high = signal_cfg["f_theta"]
    epoch_start, epoch_end = signal_cfg["epoch_window_s"]
    signal = Signal(
        fs=int(signal_cfg["fs"]),
        f_theta_low=float(theta_low),
        f_theta_high=float(theta_high),
        f_lowpass=float(signal_cfg["f_lowpass"]),
        epoch_start_s=float(epoch_start),
        epoch_end_s=float(epoch_end),
    )
    if not 0 < signal.f_theta_low < signal.f_theta_high:
        raise ValueError(f"invalid theta band: {signal.f_theta_low}-{signal.f_theta_high}")
    if signal.f_lowpass >= signal.fs / 2:
        raise ValueError("lowpass cutoff must be below Nyquist")

    thresholds = BycycleThresholds(**data["bycycle_thresholds"])

    analysis_cfg = data.get("analysis", {})
    conditions = analysis_cfg["conditions"]
    analysis = Analysis(
        burst_only=bool(analysis_cfg["burst_only"]),
        metrics=tuple(analysis_cfg["metrics"]),
        baseline_condition=int(conditions["baseline"]),
        comparison_condition=int(conditions["comparison"]),
        min_cycles_per_channel_load=int(analysis_cfg["min_cycles_per_channel_load"]),
        n_permutations=int(analysis_cfg["n_permutations"]),
        random_seed=int(analysis_cfg["random_seed"]),
        symmetry_null_value=float(analysis_cfg["symmetry_null_value"]),
    )
    if analysis.baseline_condition == analysis.comparison_condition:
        raise ValueError("baseline and comparison conditions must differ")
    if analysis.n_permutations < 1000:
        raise ValueError("n_permutations below 1000 gives a p-value resolution too coarse to report")

    dq_cfg = data.get("dq", {})
    lower, upper = dq_cfg["symmetry_bounds"]
    dq = DataQuality(
        max_null_fraction=float(dq_cfg["max_null_fraction"]),
        max_channel_dropout_fraction=float(dq_cfg["max_channel_dropout_fraction"]),
        symmetry_lower_bound=float(lower),
        symmetry_upper_bound=float(upper),
        min_rows_per_file=int(dq_cfg["min_rows_per_file"]),
    )

    return Config(
        paths=paths,
        signal=signal,
        thresholds=thresholds,
        analysis=analysis,
        dq=dq,
    )
