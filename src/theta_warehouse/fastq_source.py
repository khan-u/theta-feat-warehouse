"""This module reads FASTQ sequencing reads and reduces each to per-read metrics.

FASTQ is the community format for raw sequencing reads: four lines per read, a
``@`` header, the base sequence, a ``+`` separator, and a per-base quality string
whose characters encode Phred scores as ``ord(char) - phred_offset`` (33 for
Sanger / Illumina 1.8+). It is the genomics analogue of the NWB LFP this
warehouse already ingests: a documented scientific format that reduces to a
tidy, columnar per-record table.

This file holds only the pure reduction: parse the four-line records, decode
quality, and compute the per-read metrics (length, GC content, N count, mean and
range of Phred). It has no I/O and no database dependency, so it is unit-tested
directly. The file discovery, gzip handling, and Parquet write live alongside it
but are kept separate from these helpers.

Reads whose quality string does not match the sequence length, or that contain
bases outside the configured alphabet, are *malformed*: ``validate_record``
reports the reason and the ingest step counts and drops them, the same way
``nwb_source`` skips and counts trial-channels it cannot process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator

# The per-read metric contract written to the Parquet lake, in emitted order.
# Lineage columns (sample_id, source_file, run_id, ingested_at) are added by the
# ingest step, the same way ingest.py appends them to the cycle features.
READ_METRICS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("read_id", "VARCHAR"),
    ("seq_length", "INTEGER"),
    ("gc_content", "DOUBLE"),
    ("n_bases", "INTEGER"),
    ("mean_phred", "DOUBLE"),
    ("min_phred", "INTEGER"),
    ("max_phred", "INTEGER"),
)

READ_METRICS_COLUMN_NAMES: tuple[str, ...] = tuple(name for name, _ in READ_METRICS_COLUMNS)


class FastqFormatError(RuntimeError):
    """Raised when a stream does not follow the four-line FASTQ structure."""


@dataclass(frozen=True)
class FastqRecord:
    """One raw FASTQ read: identifier, base sequence, and quality string."""

    read_id: str
    sequence: str
    quality: str


@dataclass(frozen=True)
class ReadMetrics:
    """Per-read reduction of a FASTQ record."""

    read_id: str
    seq_length: int
    gc_content: float
    n_bases: int
    mean_phred: float
    min_phred: int
    max_phred: int

    def as_row(self) -> tuple[object, ...]:
        """Values in READ_METRICS_COLUMNS order, for a bulk DuckDB insert."""
        return (
            self.read_id,
            self.seq_length,
            self.gc_content,
            self.n_bases,
            self.mean_phred,
            self.min_phred,
            self.max_phred,
        )


def parse_read_id(header: str) -> str:
    """Return the read identifier from a FASTQ header line.

    The header is ``@<id><space><optional description>``; the id is the first
    whitespace-delimited token with the leading ``@`` removed.
    """
    core = header[1:] if header.startswith("@") else header
    parts = core.split(None, 1)
    return parts[0] if parts else ""


def parse_fastq(lines: Iterable[str]) -> Iterator[FastqRecord]:
    """Yield FastqRecords from an iterable of lines.

    Groups the stream into four-line records. Blank lines with no record in
    progress are skipped, so a trailing newline at end of file is tolerated. A
    header not starting with ``@`` or a separator not starting with ``+`` is a
    structural error, as is a stream whose line count is not a multiple of four.
    """
    buffer: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n").rstrip("\r")
        if not line and not buffer:
            continue
        buffer.append(line)
        if len(buffer) < 4:
            continue

        header, sequence, separator, quality = buffer
        if not header.startswith("@"):
            raise FastqFormatError(f"expected '@' header, got {header[:20]!r}")
        if not separator.startswith("+"):
            raise FastqFormatError(f"expected '+' separator, got {separator[:20]!r}")
        yield FastqRecord(read_id=parse_read_id(header), sequence=sequence, quality=quality)
        buffer = []

    if buffer:
        raise FastqFormatError(
            f"truncated FASTQ: {len(buffer)} trailing line(s), not a multiple of four"
        )


def gc_content(sequence: str) -> float:
    """Fraction of G and C bases over the whole read; 0.0 for an empty read."""
    if not sequence:
        return 0.0
    upper = sequence.upper()
    return (upper.count("G") + upper.count("C")) / len(upper)


def phred_scores(quality: str, offset: int) -> list[int]:
    """Decode a quality string to Phred scores as ``ord(char) - offset``."""
    return [ord(char) - offset for char in quality]


def mean_phred(quality: str, offset: int) -> float:
    """Mean Phred score over a quality string; 0.0 for an empty string."""
    scores = phred_scores(quality, offset)
    return sum(scores) / len(scores) if scores else 0.0


def validate_record(record: FastqRecord, allowed_bases: str) -> str | None:
    """Return a reason string if the record is malformed, else None.

    Three ways a read is unusable: an empty sequence, a quality string whose
    length does not match the sequence (so scores cannot be aligned to bases),
    and a base outside the configured alphabet (a mis-encoded or corrupt read).
    """
    if len(record.sequence) == 0:
        return "empty sequence"
    if len(record.quality) != len(record.sequence):
        return "quality/sequence length mismatch"
    allowed = set(allowed_bases.upper())
    unexpected = set(record.sequence.upper()) - allowed
    if unexpected:
        return f"invalid base(s): {''.join(sorted(unexpected))}"
    return None


def compute_metrics(record: FastqRecord, offset: int) -> ReadMetrics:
    """Reduce a validated record to its per-read metrics."""
    upper = record.sequence.upper()
    scores = phred_scores(record.quality, offset)
    return ReadMetrics(
        read_id=record.read_id,
        seq_length=len(upper),
        gc_content=gc_content(upper),
        n_bases=upper.count("N"),
        mean_phred=mean_phred(record.quality, offset),
        min_phred=min(scores) if scores else 0,
        max_phred=max(scores) if scores else 0,
    )
