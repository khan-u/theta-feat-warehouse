"""Tests for FASTQ discovery and Parquet ingestion (uses DuckDB, no external data)."""
import gzip
from pathlib import Path

import duckdb
import pytest

from theta_warehouse.config import Genomics
from theta_warehouse.fastq_source import discover_fastq_files, ingest_fastq


def _genomics(tmp_path: Path) -> Genomics:
    return Genomics(
        fastq_parquet_root=tmp_path / "genomics_lake",
        phred_offset=33,
        phred_max=45,
        min_reads=1,
        allowed_bases="ACGTN",
    )


def _fastq_text(lengths_and_quality: list[tuple[str, str]]) -> str:
    lines = []
    for index, (seq, qual) in enumerate(lengths_and_quality):
        lines += [f"@read{index}", seq, "+", qual]
    return "\n".join(lines) + "\n"


def test_discover_finds_plain_and_gzip(tmp_path):
    (tmp_path / "a.fastq").write_text(_fastq_text([("ACGT", "IIII")]))
    with gzip.open(tmp_path / "b.fq.gz", "wt") as handle:
        handle.write(_fastq_text([("GGCC", "IIII")]))
    (tmp_path / "notes.txt").write_text("ignore me")
    found = discover_fastq_files([tmp_path])
    assert len(found) == 2
    assert {p.name for p in found} == {"a.fastq", "b.fq.gz"}


def test_discover_deduplicates(tmp_path):
    f = tmp_path / "a.fastq"
    f.write_text(_fastq_text([("ACGT", "IIII")]))
    assert len(discover_fastq_files([f, f])) == 1


def test_ingest_writes_parquet_roundtrip(tmp_path):
    src = tmp_path / "sample.fastq"
    src.write_text(_fastq_text([("ACGT", "IIII"), ("GGCCAA", "IIIIII"), ("AT", "!!")]))
    result = ingest_fastq(_genomics(tmp_path), [src])

    assert result.reads_written == 3
    assert result.reads_skipped == 0
    assert result.parquet_path.exists()
    assert result.parquet_path.parent.name == "sample_id=sample"

    rows = duckdb.connect().execute(
        "SELECT read_id, seq_length, gc_content FROM read_parquet(?) ORDER BY seq_length",
        [str(result.parquet_path)],
    ).fetchall()
    assert [r[1] for r in rows] == [2, 4, 6]  # written sorted by length
    by_len = {r[1]: r[2] for r in rows}
    assert by_len[6] == pytest.approx(4 / 6)  # GGCCAA -> G,G,C,C


def test_ingest_skips_malformed_reads(tmp_path):
    src = tmp_path / "sample.fastq"
    # second read has an invalid base 'X'; it must be counted and dropped.
    src.write_text(_fastq_text([("ACGT", "IIII"), ("ACXT", "IIII")]))
    result = ingest_fastq(_genomics(tmp_path), [src])
    assert result.reads_written == 1
    assert result.reads_skipped == 1
    assert "invalid base" in (result.first_skip_reason or "")


def test_ingest_respects_max_reads(tmp_path):
    src = tmp_path / "sample.fastq"
    src.write_text(_fastq_text([("ACGT", "IIII")] * 5))
    result = ingest_fastq(_genomics(tmp_path), [src], max_reads=2)
    assert result.reads_written == 2


def test_ingest_raises_when_no_files(tmp_path):
    with pytest.raises(FileNotFoundError, match="no FASTQ files"):
        ingest_fastq(_genomics(tmp_path), [tmp_path / "missing.fastq"])
