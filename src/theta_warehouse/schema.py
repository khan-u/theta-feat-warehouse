"""This module defines the column contract for the cycle-feature CSVs emitted by eeg-feat-ext.

The schema below is taken directly from ``src/RunBycycle.py`` in eeg-feat-ext:
``compute_features()`` returns the bycycle 1.1.0 feature frame, and the script
inserts ``trial``, ``channel_idx`` and ``channel_label`` at positions 0, 1, 2
before writing with ``index=False``.

Consequences that this module encodes:

* The bycycle DataFrame index (the cycle number within the signal) is *lost* on
  write. It is reconstructed downstream from ``sample_last_trough`` ordering.
* Files are written by MATLAB-adjacent tooling on Windows and carry a UTF-8 BOM,
  so the first header cell may arrive as ``\\ufefftrial``. Reader configuration
  supplies names explicitly rather than trusting the header row.
* ``amp_consistency`` and ``period_consistency`` are undefined for the first and
  last cycle of a signal, so NULLs in those columns are expected, not a defect.
"""

from __future__ import annotations

from typing import Final

# Identity columns inserted by RunBycycle.py (df.insert at 0, 1, 2).
IDENTITY_COLUMNS: Final[dict[str, str]] = {
    "trial": "INTEGER",
    "channel_idx": "INTEGER",
    "channel_label": "VARCHAR",
}

# bycycle 1.1.0 compute_features() output, in emitted order.
BYCYCLE_COLUMNS: Final[dict[str, str]] = {
    # burst-detection criteria
    "amp_fraction": "DOUBLE",
    "amp_consistency": "DOUBLE",
    "period_consistency": "DOUBLE",
    "monotonicity": "DOUBLE",
    # cycle timing (samples)
    "period": "DOUBLE",
    "time_peak": "DOUBLE",
    "time_trough": "DOUBLE",
    # cycle voltage
    "volt_peak": "DOUBLE",
    "volt_trough": "DOUBLE",
    "time_decay": "DOUBLE",
    "time_rise": "DOUBLE",
    "volt_decay": "DOUBLE",
    "volt_rise": "DOUBLE",
    "volt_amp": "DOUBLE",
    # waveform-shape symmetry: the two metrics the analysis turns on
    "time_rdsym": "DOUBLE",
    "time_ptsym": "DOUBLE",
    "band_amp": "DOUBLE",
    # sample landmarks
    "sample_peak": "BIGINT",
    "sample_last_zerox_decay": "BIGINT",
    "sample_zerox_decay": "BIGINT",
    "sample_zerox_rise": "BIGINT",
    "sample_last_trough": "BIGINT",
    "sample_next_trough": "BIGINT",
    # burst membership
    "is_burst": "BOOLEAN",
}

SOURCE_COLUMNS: Final[dict[str, str]] = {**IDENTITY_COLUMNS, **BYCYCLE_COLUMNS}

SOURCE_COLUMN_ORDER: Final[tuple[str, ...]] = tuple(SOURCE_COLUMNS)

# The two bounded symmetry measures. bycycle defines both on [0, 1] with 0.5
# meaning "symmetric", which is the null value the published control tests.
SYMMETRY_METRICS: Final[tuple[str, ...]] = ("time_ptsym", "time_rdsym")

# Values that mean NULL in files produced across MATLAB/pandas/Windows hops.
NULL_STRINGS: Final[tuple[str, ...]] = ("", "NA", "NaN", "NAN", "nan", "None", "null")


def duckdb_columns_struct() -> str:
    """Render the source columns as a DuckDB ``read_csv(columns = {...})`` literal.

    Supplying the spec explicitly means the reader ignores whatever the header
    row says, which sidesteps the BOM problem and pins types instead of letting
    the sniffer infer them per file.
    """
    body = ", ".join(f"'{name}': '{sql_type}'" for name, sql_type in SOURCE_COLUMNS.items())
    return "{" + body + "}"


def duckdb_nullstr_literal() -> str:
    """Render NULL_STRINGS as a DuckDB list literal."""
    return "[" + ", ".join(f"'{value}'" for value in NULL_STRINGS) + "]"


def missing_columns(observed: list[str]) -> list[str]:
    """Return source columns absent from ``observed`` (BOM-tolerant)."""
    normalized = {name.lstrip("\ufeff").strip() for name in observed}
    return [name for name in SOURCE_COLUMNS if name not in normalized]


def unexpected_columns(observed: list[str]) -> list[str]:
    """Return observed columns not present in the source contract."""
    normalized = [name.lstrip("\ufeff").strip() for name in observed]
    return [name for name in normalized if name not in SOURCE_COLUMNS]
