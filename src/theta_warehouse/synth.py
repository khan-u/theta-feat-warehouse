"""This module generates synthetic cycle-feature CSVs.

The clinical iEEG behind eeg-feat-ext cannot be published, and MAIN.m notes that
all subject data is omitted from that repository. So this warehouse ships a
generator that emits files byte-compatible with what RunBycycle.py writes:
same 27 columns in the same order, same filename and directory convention, same
UTF-8 BOM, same NULL pattern at signal edges, and burst runs that respect
``min_n_cycles``.

That keeps the repo runnable by anyone with one command, and it gives the data
quality checks something to be checked against. ``--effect`` injects a
load-dependent shift in the symmetry metrics, which is how the analysis code is
tested for power rather than only for correctness under the null.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from .naming import format_feature_filename
from .schema import SOURCE_COLUMN_ORDER

# Reference profile: 32 subjects, 586 hippocampal channels in total, matching the
# scale of the recordings the upstream pipeline was validated on.
FULL_SCALE = dict(n_subjects=32, n_channels_total=586, n_trials=60)
DEMO_SCALE = dict(n_subjects=3, n_channels_total=24, n_trials=20)


@dataclass(frozen=True)
class SynthSpec:
    n_subjects: int
    n_channels_total: int
    n_trials: int
    regions: tuple[str, ...] = ("HP",)
    loads: tuple[int, ...] = (1, 3)
    fs: int = 400
    epoch_start_s: float = -0.3
    epoch_end_s: float = 2.8
    theta_hz: float = 5.0
    effect: float = 0.0
    burst_fraction: float = 0.6
    min_n_cycles: int = 3
    seed: int = 20240115

    @property
    def epoch_duration_s(self) -> float:
        return self.epoch_end_s - self.epoch_start_s


def _allocate_channels(n_channels_total: int, n_subjects: int, rng: np.random.Generator) -> list[int]:
    """Split a channel budget across subjects with realistic imbalance.

    Electrode coverage varies per patient, so an even split would be the one
    distribution the real data never has. Every subject gets at least one
    channel and the totals sum exactly.
    """
    if n_channels_total < n_subjects:
        raise ValueError("need at least one channel per subject")
    weights = rng.dirichlet(np.full(n_subjects, 3.0))
    counts = np.maximum(1, np.floor(weights * n_channels_total).astype(int))
    deficit = n_channels_total - int(counts.sum())
    while deficit != 0:
        idx = rng.integers(0, n_subjects)
        if deficit > 0:
            counts[idx] += 1
            deficit -= 1
        elif counts[idx] > 1:
            counts[idx] -= 1
            deficit += 1
    return counts.tolist()


def _burst_mask(n_cycles: int, fraction: float, min_run: int, rng: np.random.Generator) -> np.ndarray:
    """Boolean burst membership in contiguous runs of at least ``min_run``.

    bycycle only labels a cycle as part of a burst when consecutive cycles pass
    the amplitude and period consistency thresholds together, so independent
    Bernoulli draws would not reproduce that contiguous structure.
    """
    mask = np.zeros(n_cycles, dtype=bool)
    if n_cycles < min_run:
        return mask
    target = int(round(fraction * n_cycles))
    cursor = 0
    while mask.sum() < target and cursor < n_cycles:
        run = int(rng.integers(min_run, max(min_run + 1, min_run + 5)))
        start = cursor + int(rng.integers(0, 3))
        end = min(start + run, n_cycles)
        if end - start >= min_run:
            mask[start:end] = True
        cursor = end + int(rng.integers(1, 3))
    return mask


def _symmetry_draw(
    n: int,
    center: float,
    spread: float,
    rng: np.random.Generator,
) -> np.ndarray:
    """Draw a bounded symmetry metric on (0, 1) centred near ``center``.

    A Beta parameterised by its mean keeps values inside the open interval, which
    matters because a Gaussian would occasionally produce out-of-range values and
    silently trip the range check that exists to catch real corruption.
    """
    center = float(np.clip(center, 0.02, 0.98))
    concentration = max(2.0, (center * (1.0 - center)) / max(spread**2, 1e-6) - 1.0)
    a = center * concentration
    b = (1.0 - center) * concentration
    return rng.beta(a, b, size=n)


def _cycle_frame(
    n_cycles: int,
    spec: SynthSpec,
    ptsym_center: float,
    rdsym_center: float,
    rng: np.random.Generator,
) -> dict[str, np.ndarray]:
    """Build one trial x channel block of cycle features."""
    period_samples = spec.fs / spec.theta_hz
    period = rng.normal(period_samples, period_samples * 0.08, size=n_cycles).clip(
        spec.fs / 12.0, spec.fs / 2.0
    )

    time_ptsym = _symmetry_draw(n_cycles, ptsym_center, 0.06, rng)
    time_rdsym = _symmetry_draw(n_cycles, rdsym_center, 0.06, rng)

    time_peak = period * time_ptsym
    time_trough = period - time_peak
    time_rise = period * time_rdsym
    time_decay = period - time_rise

    volt_amp = rng.gamma(shape=4.0, scale=12.0, size=n_cycles)
    volt_peak = volt_amp / 2.0 + rng.normal(0, 2.0, size=n_cycles)
    volt_trough = -volt_amp / 2.0 + rng.normal(0, 2.0, size=n_cycles)
    band_amp = volt_amp * rng.uniform(0.7, 1.1, size=n_cycles)

    # Sample landmarks: cumulative so they increase monotonically within a signal.
    last_trough = np.concatenate([[0.0], np.cumsum(period)[:-1]]).round()
    next_trough = (last_trough + period).round()
    sample_peak = (last_trough + time_rise).round()
    zerox_rise = (last_trough + time_rise * 0.5).round()
    zerox_decay = (sample_peak + time_decay * 0.5).round()
    last_zerox_decay = np.concatenate([[0.0], zerox_decay[:-1]]).round()

    amp_fraction = rng.uniform(0.0, 1.0, size=n_cycles)
    monotonicity = rng.beta(6.0, 2.0, size=n_cycles)
    amp_consistency = rng.beta(5.0, 2.0, size=n_cycles)
    period_consistency = rng.beta(6.0, 2.0, size=n_cycles)

    # bycycle cannot define consistency for the first and last cycle of a signal.
    amp_consistency[0] = np.nan
    period_consistency[0] = np.nan
    if n_cycles > 1:
        amp_consistency[-1] = np.nan
        period_consistency[-1] = np.nan

    is_burst = _burst_mask(n_cycles, spec.burst_fraction, spec.min_n_cycles, rng)
    # A cycle with an undefined consistency criterion can never be in a burst.
    is_burst &= ~np.isnan(amp_consistency)
    is_burst &= ~np.isnan(period_consistency)

    return {
        "amp_fraction": amp_fraction,
        "amp_consistency": amp_consistency,
        "period_consistency": period_consistency,
        "monotonicity": monotonicity,
        "period": period,
        "time_peak": time_peak,
        "time_trough": time_trough,
        "volt_peak": volt_peak,
        "volt_trough": volt_trough,
        "time_decay": time_decay,
        "time_rise": time_rise,
        "volt_decay": volt_amp * rng.uniform(0.4, 0.6, size=n_cycles),
        "volt_rise": volt_amp * rng.uniform(0.4, 0.6, size=n_cycles),
        "volt_amp": volt_amp,
        "time_rdsym": time_rdsym,
        "time_ptsym": time_ptsym,
        "band_amp": band_amp,
        "sample_peak": sample_peak,
        "sample_last_zerox_decay": last_zerox_decay,
        "sample_zerox_decay": zerox_decay,
        "sample_zerox_rise": zerox_rise,
        "sample_last_trough": last_trough,
        "sample_next_trough": next_trough,
        "is_burst": is_burst,
    }


def _format_value(column: str, value: object) -> str:
    """Render a value the way pandas' ``to_csv`` would."""
    if column == "is_burst":
        return "True" if bool(value) else "False"
    if column.startswith("sample_"):
        return str(int(value))
    if isinstance(value, float) and np.isnan(value):
        return ""  # pandas writes NaN as an empty field
    return f"{float(value):.6f}"


def generate(
    spec: SynthSpec,
    source_root: Path,
    trial_metadata_root: Path,
    extracted_at: datetime | None = None,
) -> dict[str, int]:
    """Write synthetic cycle-feature CSVs and trial metadata.

    Returns a small summary dict so callers (CLI, tests) can assert on volume
    without re-reading the files.
    """
    rng = np.random.default_rng(spec.seed)
    extracted_at = extracted_at or datetime(2025, 6, 1, 9, 15, 0)

    source_root.mkdir(parents=True, exist_ok=True)
    trial_metadata_root.mkdir(parents=True, exist_ok=True)

    channels_per_subject = _allocate_channels(spec.n_channels_total, spec.n_subjects, rng)
    subject_ids = [f"P{60 + i}cs" for i in range(spec.n_subjects)]

    expected_cycles = int(spec.epoch_duration_s * spec.theta_hz)
    totals = {"files": 0, "rows": 0, "channels": 0, "trials": 0}

    for subject_index, (subject_id, n_channels) in enumerate(
        zip(subject_ids, channels_per_subject)
    ):
        # Trial metadata: load condition alternates in blocks, as in a task with
        # interleaved set sizes rather than a single switch partway through.
        trial_loads = np.array(
            [spec.loads[(t // 2) % len(spec.loads)] for t in range(spec.n_trials)]
        )
        correct = rng.random(spec.n_trials) < 0.82

        trial_path = trial_metadata_root / f"{subject_id}_trial_metadata.csv"
        with trial_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["subject_id", "trial", "load_condition", "correct"])
            for trial in range(spec.n_trials):
                writer.writerow(
                    [subject_id, trial, int(trial_loads[trial]), "True" if correct[trial] else "False"]
                )
        totals["trials"] += spec.n_trials

        # Per-subject channel-label pool, mirroring the LH1/RH2 style in the
        # sample file and the [RL] prefix in MAIN.m's region regexes.
        labels = [
            f"{'L' if c % 2 == 0 else 'R'}H{c // 2 + 1}" for c in range(n_channels)
        ]

        for region in spec.regions:
            region_dir = source_root / subject_id / region
            region_dir.mkdir(parents=True, exist_ok=True)
            filename = format_feature_filename(
                subject_id,
                region,
                extracted_at + timedelta(minutes=subject_index),
            )
            out_path = region_dir / filename

            rows_written = 0
            # utf-8-sig reproduces the BOM the real files carry.
            with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(SOURCE_COLUMN_ORDER)

                for channel_idx, channel_label in enumerate(labels):
                    # A per-channel offset makes channels differ from each other,
                    # so a channel-level aggregate reflects real per-channel
                    # variation rather than noise.
                    channel_bias = rng.normal(0.0, 0.015)
                    for trial in range(spec.n_trials):
                        load = int(trial_loads[trial])
                        shift = spec.effect if load == spec.loads[-1] else 0.0
                        n_cycles = max(
                            spec.min_n_cycles,
                            int(rng.poisson(expected_cycles)),
                        )
                        block = _cycle_frame(
                            n_cycles,
                            spec,
                            ptsym_center=0.5 + channel_bias + shift,
                            rdsym_center=0.5 + channel_bias + shift * 0.5,
                            rng=rng,
                        )
                        for i in range(n_cycles):
                            record = [trial, channel_idx, channel_label]
                            for column in SOURCE_COLUMN_ORDER[3:]:
                                record.append(_format_value(column, block[column][i]))
                            writer.writerow(record)
                            rows_written += 1

            totals["files"] += 1
            totals["rows"] += rows_written
            totals["channels"] += n_channels

    return totals


def spec_from_profile(profile: str, **overrides: object) -> SynthSpec:
    """Build a :class:`SynthSpec` from a named size profile."""
    base = {"demo": DEMO_SCALE, "full": FULL_SCALE}.get(profile)
    if base is None:
        raise ValueError(f"unknown profile {profile!r}; expected 'demo' or 'full'")
    merged: dict[str, object] = {**base, **{k: v for k, v in overrides.items() if v is not None}}
    return SynthSpec(**merged)  # type: ignore[arg-type]
