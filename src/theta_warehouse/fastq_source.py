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

import gzip
import shutil
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .config import Genomics
from .db import sql_string_literal

# Extensions treated as FASTQ, plain or gzip-compressed. ENA/SRA distribute reads
# gzip-compressed, so .gz is the common case rather than the exception.
FASTQ_SUFFIXES: tuple[str, ...] = (".fastq", ".fq", ".fastq.gz", ".fq.gz")

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


# ------------------------------------------------------------------ file input


def _has_fastq_suffix(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(suffix) for suffix in FASTQ_SUFFIXES)


def discover_fastq_files(paths: list[Path]) -> list[Path]:
    """Expand file and directory arguments into a sorted, de-duplicated list.

    Directories are searched recursively so a run folder can be pointed at
    directly. Mirrors ``nwb_source.discover_lfp_files`` for the FASTQ suffixes.
    """
    found: list[Path] = []
    for entry in paths:
        entry = entry.expanduser()
        if entry.is_dir():
            found.extend(p for p in sorted(entry.rglob("*")) if p.is_file() and _has_fastq_suffix(p.name))
        elif entry.is_file() and _has_fastq_suffix(entry.name):
            found.append(entry)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        resolved = str(path.resolve())
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


@contextmanager
def open_reads(path: Path):
    """Open a FASTQ file as text, transparently decompressing ``.gz``."""
    if path.name.lower().endswith(".gz"):
        handle = gzip.open(path, "rt", encoding="utf-8")
    else:
        handle = path.open("r", encoding="utf-8")
    try:
        yield handle
    finally:
        handle.close()


def iter_records(path: Path) -> Iterator[FastqRecord]:
    """Yield FastqRecords from one FASTQ file (plain or gzip)."""
    with open_reads(path) as handle:
        yield from parse_fastq(handle)


def _sample_id_from_path(path: Path) -> str:
    """Strip FASTQ suffixes from a filename to use as a sample identifier."""
    name = path.name
    for suffix in sorted(FASTQ_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


@dataclass
class IngestResult:
    sample_id: str
    parquet_path: Path
    reads_written: int = 0
    reads_skipped: int = 0
    source_files: list[str] = field(default_factory=list)
    first_skip_reason: str | None = None

    def summary(self) -> dict[str, object]:
        summary: dict[str, object] = {
            "sample_id": self.sample_id,
            "reads_written": self.reads_written,
            "reads_skipped": self.reads_skipped,
            "files": len(self.source_files),
        }
        if self.first_skip_reason is not None:
            summary["first_skip_reason"] = self.first_skip_reason
        return summary


def _partition_dir(genomics: Genomics, sample_id: str) -> Path:
    return genomics.fastq_parquet_root / f"sample_id={sample_id}"


def ingest_fastq(
    genomics: Genomics,
    paths: list[Path],
    sample_id: str | None = None,
    run_id: str | None = None,
    max_reads: int | None = None,
    ingested_at: datetime | None = None,
) -> IngestResult:
    """Reduce FASTQ files to per-read metrics and write one Parquet partition.

    Metrics are computed here in Python, then bulk-inserted into a DuckDB
    in-memory table and written to Parquet with ``COPY``. This keeps the write
    columnar and typed without a pandas dependency, and sorting by ``seq_length``
    puts similar-length reads in contiguous row groups so length predicates prune
    by row-group statistics, the same approach the cycle-feature loader uses for
    channels. Writing is delete-then-write at partition grain, so a re-run
    replaces exactly this sample and leaves other samples untouched.
    """
    import duckdb

    files = discover_fastq_files(paths)
    if not files:
        raise FileNotFoundError(f"no FASTQ files found in: {', '.join(str(p) for p in paths)}")

    sample_id = sample_id or _sample_id_from_path(files[0])
    run_id = run_id or f"local__{uuid.uuid4().hex[:12]}"
    ingested_at = ingested_at or datetime.now(timezone.utc)
    offset = genomics.phred_offset

    result = IngestResult(sample_id=sample_id, parquet_path=Path())
    rows: list[tuple[object, ...]] = []
    reached_cap = False
    for path in files:
        result.source_files.append(str(path))
        for record in iter_records(path):
            if max_reads is not None and result.reads_written >= max_reads:
                reached_cap = True
                break
            reason = validate_record(record, genomics.allowed_bases)
            if reason is not None:
                result.reads_skipped += 1
                if result.first_skip_reason is None:
                    result.first_skip_reason = reason
                continue
            metrics = compute_metrics(record, offset)
            rows.append(metrics.as_row() + (sample_id, str(path), run_id, ingested_at))
            result.reads_written += 1
        if reached_cap:
            break

    target_dir = _partition_dir(genomics, sample_id)
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_file = target_dir / "part-0.parquet"
    result.parquet_path = target_file

    all_columns = list(READ_METRICS_COLUMNS) + [
        ("sample_id", "VARCHAR"),
        ("source_file", "VARCHAR"),
        ("run_id", "VARCHAR"),
        ("ingested_at", "TIMESTAMP"),
    ]
    ddl = ", ".join(f"{name} {sql_type}" for name, sql_type in all_columns)
    placeholders = ", ".join(["?"] * len(all_columns))

    connection = duckdb.connect()
    try:
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute(f"CREATE TABLE reads ({ddl})")
        if rows:
            connection.executemany(f"INSERT INTO reads VALUES ({placeholders})", rows)
        connection.execute(
            f"""
            COPY (SELECT * FROM reads ORDER BY seq_length, read_id)
            TO {sql_string_literal(str(target_file))} (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
    finally:
        connection.close()

    return result
