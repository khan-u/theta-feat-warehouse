"""This module bridges SBCAT (Daume et al.) NWB files to the eeg-feat-ext CSV contract.

The warehouse consumes the cycle-feature CSVs that eeg-feat-ext's
``RunBycycle.py`` writes. Upstream of those CSVs, eeg-feat-ext runs a MATLAB
preprocessing stage that pulls per-trial LFP out of raw recordings, removes
spike potentials, downsamples to 400 Hz, and epochs each trial.

The SBCAT release (DANDI 000673, Daume et al., "Control of working memory
maintenance by theta-gamma phase-amplitude coupling of human hippocampal
neurons") ships that intermediate product inside the NWB file: an
``LFPs`` ElectricalSeries described as "spike potentials removed and downsampled
to 400Hz". That means the whole MATLAB stage can be skipped and the LFP read
straight from NWB, then handed to the same neurodsp lowpass + bycycle feature
extraction ``RunBycycle.py`` performs.

This module reproduces that extraction:

    LFP (samples x channels, 400 Hz)   from /acquisition/LFPs
      -> per trial, epoch [-0.3, 2.8] s around timestamps_Maintenance
      -> lowpass at f_lowpass Hz (neurodsp)
      -> bycycle.compute_features(sig, fs, f_theta, thresholds)
      -> the 27-column CSV, one file per (subject, region)

plus the trial-metadata CSV (load condition, accuracy) the paired analysis
needs, read from the NWB trials table. The output is byte-compatible with what
the warehouse already loads, so real data and synthetic data take the same path
through the pipeline.

Reading is done with h5py rather than pynwb: the file layout is fixed and known,
the LFP series is large, and h5py reads the exact datasets needed without
materialising the whole file through the pynwb object model.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

from .config import Config
from .naming import format_feature_filename
from .schema import SOURCE_COLUMN_ORDER

# Condensed, filename-safe region codes for the SBCAT electrode ``location``
# strings. Mirrors condenseAreas.m / translateArea_SB.m in the SBCAT release.
# Region codes must be alphanumeric to satisfy the feature-file naming contract.
REGION_CODE_BY_BASE = {
    "hippocampus": "Hipp",
    "amygdala": "Amg",
    "dorsal_anterior_cingulate_cortex": "dACC",
    "pre_supplementary_motor_area": "preSMA",
    "ventral_medial_prefrontal_cortex": "vmPFC",
}

LFP_ACQUISITION_NAME = "LFPs"


class NwbFormatError(RuntimeError):
    """Raised when an NWB file does not match the SBCAT LFP layout."""


@dataclass(frozen=True)
class ChannelInfo:
    """One LFP channel: its column in the LFP matrix, region and label."""

    column: int
    region_code: str
    hemisphere: str
    label: str
    location: str
    orig_channel: int


@dataclass
class ExtractionResult:
    subject_id: str
    session_id: str
    files_written: list[Path] = field(default_factory=list)
    trial_metadata_path: Path | None = None
    n_trials: int = 0
    n_channels: int = 0
    cycle_rows: int = 0
    skipped_trial_channels: int = 0
    first_error: str | None = None

    def summary(self) -> dict[str, object]:
        summary: dict[str, object] = {
            "subject_id": self.subject_id,
            "session_id": self.session_id,
            "regions": len(self.files_written),
            "trials": self.n_trials,
            "channels": self.n_channels,
            "cycle_rows": self.cycle_rows,
            "skipped_trial_channels": self.skipped_trial_channels,
        }
        if self.first_error is not None:
            summary["first_error"] = self.first_error
        return summary


def _decode(value: object) -> str:
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8")
    return str(value)


def _condense_location(location: str) -> tuple[str, str]:
    """Return ``(region_code, hemisphere)`` for an electrode location string.

    Locations look like ``hippocampus_right`` or
    ``dorsal_anterior_cingulate_cortex_left``. The hemisphere is the trailing
    ``_left``/``_right``; the remainder maps to a condensed region code.
    """
    hemisphere = ""
    base = location
    for suffix, code in (("_left", "L"), ("_right", "R")):
        if location.endswith(suffix):
            hemisphere = code
            base = location[: -len(suffix)]
            break

    region_code = REGION_CODE_BY_BASE.get(base)
    if region_code is None:
        # Fall back to an alphanumeric squeeze of the base so an unmapped
        # region is still ingestible rather than silently dropped.
        region_code = "".join(part[:1].upper() + part[1:] for part in base.split("_"))
        region_code = "".join(ch for ch in region_code if ch.isalnum()) or "NA"
    return region_code, hemisphere


def _subject_and_session(identifier: str, subject_id_field: str, session_id_field: str) -> tuple[str, str]:
    """Derive a stable subject id and session id from the NWB metadata.

    The file identifier looks like ``sub-10_ses-1_P68CS``. The trailing token is
    the patient code (``P68CS``), which is the identity the eeg-feat-ext
    convention uses and is stable across the two DANDI releases, unlike the
    ``sub-NN`` number, which is reassigned per release.
    """
    token = identifier.split("_")[-1] if identifier else ""
    if token and token.upper().startswith("P"):
        subject_id = token
    elif subject_id_field:
        subject_id = f"sub{subject_id_field}"
    else:
        subject_id = identifier or "unknown"
    return subject_id, session_id_field or "1"


def read_channels(h5file) -> list[ChannelInfo]:
    """Build the channel list from the LFP electrode region and electrodes table."""
    acquisition = h5file["acquisition"]
    if LFP_ACQUISITION_NAME not in acquisition:
        raise NwbFormatError(
            f"no '{LFP_ACQUISITION_NAME}' ElectricalSeries in /acquisition; "
            "this file has no continuous LFP (it is likely a spikes-only release)"
        )

    region_index = acquisition[f"{LFP_ACQUISITION_NAME}/electrodes"][:]
    electrodes = h5file["general/extracellular_ephys/electrodes"]
    locations = [_decode(v) for v in electrodes["location"][:]]
    orig_channels = electrodes["origChannel"][:]

    channels: list[ChannelInfo] = []
    running_by_region: dict[str, int] = {}
    for column, electrode_row in enumerate(region_index):
        location = locations[int(electrode_row)]
        region_code, hemisphere = _condense_location(location)
        key = f"{hemisphere}{region_code}"
        running_by_region[key] = running_by_region.get(key, 0) + 1
        label = f"{hemisphere}{region_code}{running_by_region[key]}"
        channels.append(
            ChannelInfo(
                column=column,
                region_code=region_code,
                hemisphere=hemisphere,
                label=label,
                location=location,
                orig_channel=int(round(float(orig_channels[int(electrode_row)]))),
            )
        )
    return channels


def _epoch_bounds(onset: float, start_time: float, fs: float, epoch_start_s: float, epoch_end_s: float, n_samples: int) -> tuple[int, int]:
    """Sample index range for ``t in [onset+epoch_start_s, onset+epoch_end_s)``.

    ``t_i = start_time + i / fs``; the half-open interval matches the boolean
    time mask eeg-feat-ext and the SBCAT sample code apply.
    """
    low = onset + epoch_start_s
    high = onset + epoch_end_s
    i_low = int(np.ceil((low - start_time) * fs - 1e-9))
    i_high = int(np.ceil((high - start_time) * fs - 1e-9))
    i_low = max(0, i_low)
    i_high = min(n_samples, i_high)
    return i_low, i_high


def _bycycle_feature_frame(signal: np.ndarray, fs: int, f_lowpass: float, f_theta: tuple[float, float], thresholds: dict[str, float]):
    """Lowpass then run bycycle, matching RunBycycle.get_features.

    Imported lazily so the module imports without neurodsp/bycycle present; they
    are only needed when real NWB data is actually being processed.
    """
    from bycycle.features import compute_features
    from neurodsp.filt import filter_signal

    signal_low = filter_signal(signal, fs, "lowpass", f_lowpass, remove_edges=False)
    frame = compute_features(signal_low, fs, f_theta, threshold_kwargs=thresholds)
    return frame


def _format_cell(column: str, value: object) -> str:
    """Render one feature value the way pandas' ``to_csv`` would.

    Keeps the CSV byte-compatible with what RunBycycle.py writes and what the
    synthetic generator emits, so all three share one reader.
    """
    if column == "is_burst":
        return "True" if bool(value) else "False"
    if isinstance(value, float) and np.isnan(value):
        return ""
    if column.startswith("sample_"):
        if value is None or (isinstance(value, float) and np.isnan(value)):
            return ""
        return str(int(value))
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return "" if value is None else str(value)
    if np.isnan(numeric):
        return ""
    return f"{numeric:.6f}"


def extract_file(
    nwb_path: Path,
    config: Config,
    extracted_at: datetime | None = None,
    regions: tuple[str, ...] | None = None,
    max_trials: int | None = None,
) -> ExtractionResult:
    """Extract cycle features and trial metadata from one SBCAT NWB file.

    Writes one CSV per (subject, region) under ``config.paths.source_root`` and a
    ``<subject>_trial_metadata.csv`` under ``config.paths.trial_metadata``,
    reproducing the eeg-feat-ext output contract.
    """
    import h5py

    fs = config.signal.fs
    f_lowpass = config.signal.f_lowpass
    f_theta = (config.signal.f_theta_low, config.signal.f_theta_high)
    epoch_start_s = config.signal.epoch_start_s
    epoch_end_s = config.signal.epoch_end_s
    thresholds = {
        "amp_fraction_threshold": config.thresholds.amp_fraction_threshold,
        "amp_consistency_threshold": config.thresholds.amp_consistency_threshold,
        "period_consistency_threshold": config.thresholds.period_consistency_threshold,
        "monotonicity_threshold": config.thresholds.monotonicity_threshold,
        "min_n_cycles": config.thresholds.min_n_cycles,
    }
    bycycle_columns = list(SOURCE_COLUMN_ORDER[3:])

    with h5py.File(nwb_path, "r") as h5file:
        identifier = _decode(h5file["identifier"][()]) if "identifier" in h5file else nwb_path.stem
        subject_field = _decode(h5file["general/subject/subject_id"][()]) if "general/subject/subject_id" in h5file else ""
        session_field = _decode(h5file["general/session_id"][()]) if "general/session_id" in h5file else ""
        subject_id, session_id = _subject_and_session(identifier, subject_field, session_field)

        channels = read_channels(h5file)

        lfp_series = h5file[f"acquisition/{LFP_ACQUISITION_NAME}"]
        lfp = lfp_series["data"]
        n_samples = lfp.shape[0]
        starting_time_node = lfp_series["starting_time"]
        start_time = float(starting_time_node[()])
        rate = float(starting_time_node.attrs.get("rate", fs))
        if int(round(rate)) != fs:
            raise NwbFormatError(
                f"LFP sampling rate {rate} Hz does not match configured fs {fs} Hz; "
                "update config/pipeline.yml or resample before ingest"
            )

        trials = h5file["intervals/trials"]
        maintenance = trials["timestamps_Maintenance"][:]
        loads = trials["loads"][:]
        accuracy = trials["response_accuracy"][:]
        n_trials = len(maintenance)
        if max_trials is not None:
            n_trials = min(n_trials, max_trials)

        # Load only the sample span actually used, once per trial, rather than
        # the whole multi-hundred-MB matrix.
        result = ExtractionResult(subject_id=subject_id, session_id=session_id)
        result.n_trials = n_trials
        result.n_channels = len(channels)

        extracted_at = extracted_at or datetime(2025, 6, 1, 9, 15, 0)
        session_timestamp = extracted_at

        selected_regions = None if regions is None else set(regions)
        channels_by_region: dict[str, list[ChannelInfo]] = {}
        for channel in channels:
            if selected_regions is not None and channel.region_code not in selected_regions:
                continue
            channels_by_region.setdefault(channel.region_code, []).append(channel)

        # Pre-slice each trial's LFP window for all channels at once.
        trial_windows: list[np.ndarray | None] = []
        for trial_index in range(n_trials):
            i_low, i_high = _epoch_bounds(
                float(maintenance[trial_index]), start_time, fs, epoch_start_s, epoch_end_s, n_samples
            )
            if i_high - i_low <= 0:
                trial_windows.append(None)
                continue
            window = np.asarray(lfp[i_low:i_high, :], dtype=np.float64)
            trial_windows.append(window)

        config.paths.source_root.mkdir(parents=True, exist_ok=True)
        config.paths.trial_metadata.mkdir(parents=True, exist_ok=True)

        for region_code, region_channels in sorted(channels_by_region.items()):
            region_dir = config.paths.source_root / subject_id / region_code
            region_dir.mkdir(parents=True, exist_ok=True)
            filename = format_feature_filename(subject_id, region_code, session_timestamp)
            out_path = region_dir / filename

            rows_written = 0
            with out_path.open("w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.writer(handle)
                writer.writerow(SOURCE_COLUMN_ORDER)

                for trial_index in range(n_trials):
                    window = trial_windows[trial_index]
                    if window is None:
                        continue
                    for channel in region_channels:
                        signal = window[:, channel.column]
                        if not np.isfinite(signal).all():
                            result.skipped_trial_channels += 1
                            continue
                        try:
                            frame = _bycycle_feature_frame(signal, fs, f_lowpass, f_theta, thresholds)
                        except Exception as exc:
                            if result.first_error is None:
                                result.first_error = f"{type(exc).__name__}: {exc}"
                            result.skipped_trial_channels += 1
                            continue
                        if frame is None or len(frame) == 0:
                            result.skipped_trial_channels += 1
                            continue

                        frame = frame.reindex(columns=bycycle_columns)
                        for _, feature_row in frame.iterrows():
                            record = [trial_index, channel.column, channel.label]
                            for column in bycycle_columns:
                                record.append(_format_cell(column, feature_row[column]))
                            writer.writerow(record)
                            rows_written += 1

            result.files_written.append(out_path)
            result.cycle_rows += rows_written

        # Trial metadata: 0-based trial index to match the feature files.
        trial_path = config.paths.trial_metadata / f"{subject_id}_trial_metadata.csv"
        with trial_path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["subject_id", "trial", "load_condition", "correct"])
            for trial_index in range(n_trials):
                correct = "True" if int(accuracy[trial_index]) == 1 else "False"
                writer.writerow([subject_id, trial_index, int(loads[trial_index]), correct])
        result.trial_metadata_path = trial_path

    return result


def discover_lfp_files(paths: list[Path]) -> list[Path]:
    """Expand file and directory arguments into a sorted list of NWB files.

    Directories are searched recursively, because ``dandi download`` lays the
    dataset out one subject per folder
    (``000673/sub-XX/sub-XX_ses-Y_ecephys+image.nwb``); pointing this at the
    dataset root therefore finds every session, not just files sitting directly
    in the top-level directory.
    """
    found: list[Path] = []
    for entry in paths:
        entry = entry.expanduser()
        if entry.is_dir():
            found.extend(sorted(entry.rglob("*.nwb")))
        elif entry.is_file():
            found.append(entry)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def file_has_lfp(nwb_path: Path) -> bool:
    """Return True if the NWB file contains an LFP ElectricalSeries."""
    import h5py

    with h5py.File(nwb_path, "r") as h5file:
        acquisition = h5file.get("acquisition")
        return bool(acquisition is not None and LFP_ACQUISITION_NAME in acquisition)
