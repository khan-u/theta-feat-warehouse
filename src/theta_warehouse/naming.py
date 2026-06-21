"""This module parses the file and directory conventions eeg-feat-ext writes.

RunBycycle.py writes two kinds of CSV under ``data/cycle_features``::

    <root>/<subject>/<region>/<subject>_<region>_bycycle_features_<YYYYMMDD_HHMMSS>.csv
    <root>/<subject>/<subject>_merged_bycycle_features_<YYYYMMDD_HHMMSS>.csv

Only the first kind is ingested. The merged file is a concatenation of the
region files, so loading both would double every row. The repository also ships
a placeholder (``..._YYYYMMDD_#####_sample.csv``) whose cells contain dtype
names rather than values; it fails the timestamp pattern and is skipped.

Because RunBycycle.py re-runs write a *new* timestamped file rather than
overwriting, the same (subject, region) pair can have several files. The loader
keeps every file for lineage and the fact build keeps only the newest
extraction per pair, mirroring the "retain only the latest merged CSV" rule the
upstream script already applies to its own merged output.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Subject IDs may contain underscores (e.g. "P60cs", "P_42"); region codes come
# from the brainRegions struct in MAIN.m and do not (HP, A, SMA, OF, AC).
FEATURE_FILE_PATTERN = re.compile(
    r"^(?P<subject_id>.+)_(?P<region>[A-Za-z0-9]+)_bycycle_features_"
    r"(?P<timestamp>\d{8}_\d{6})\.csv$"
)

MERGED_FILE_PATTERN = re.compile(
    r"^(?P<subject_id>.+)_merged_bycycle_features_(?P<timestamp>\d{8}_\d{6})\.csv$"
)

TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"


class NamingError(ValueError):
    """Raised when a path does not match the eeg-feat-ext convention."""


@dataclass(frozen=True)
class SourceFile:
    """A single region-level cycle-feature CSV."""

    path: Path
    subject_id: str
    region: str
    extracted_at: datetime

    @property
    def partition_key(self) -> tuple[str, str]:
        return (self.subject_id, self.region)

    @property
    def extraction_id(self) -> str:
        return self.extracted_at.strftime(TIMESTAMP_FORMAT)


def format_feature_filename(subject_id: str, region: str, extracted_at: datetime) -> str:
    """Inverse of :func:`parse_feature_filename`; used by the synthetic generator."""
    stamp = extracted_at.strftime(TIMESTAMP_FORMAT)
    return f"{subject_id}_{region}_bycycle_features_{stamp}.csv"


def is_merged_file(name: str) -> bool:
    return MERGED_FILE_PATTERN.match(name) is not None


def parse_feature_filename(name: str) -> tuple[str, str, datetime]:
    """Return ``(subject_id, region, extracted_at)`` for a region feature file."""
    match = FEATURE_FILE_PATTERN.match(name)
    if match is None:
        raise NamingError(f"not a region cycle-feature filename: {name!r}")
    if match.group("region") == "merged":
        raise NamingError(f"merged session file, not a region file: {name!r}")
    timestamp = datetime.strptime(match.group("timestamp"), TIMESTAMP_FORMAT)
    return match.group("subject_id"), match.group("region"), timestamp


def parse_source_path(path: Path, root: Path) -> SourceFile:
    """Parse a path and cross-check it against its directory position.

    The filename and the directory layout independently encode subject and
    region. Disagreement means a file was moved or hand-copied, which would
    silently mislabel rows, so it is an error rather than a warning.
    """
    subject_id, region, extracted_at = parse_feature_filename(path.name)

    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError as exc:  # pragma: no cover - defensive
        raise NamingError(f"{path} is not under source root {root}") from exc

    parts = relative.parts
    if len(parts) != 3:
        raise NamingError(
            f"expected <root>/<subject>/<region>/<file>.csv, got {relative.as_posix()!r}"
        )

    dir_subject, dir_region, _ = parts
    if dir_subject != subject_id:
        raise NamingError(
            f"subject mismatch: directory says {dir_subject!r}, filename says {subject_id!r}"
        )
    if dir_region != region:
        raise NamingError(
            f"region mismatch: directory says {dir_region!r}, filename says {region!r}"
        )

    return SourceFile(
        path=path,
        subject_id=subject_id,
        region=region,
        extracted_at=extracted_at,
    )
