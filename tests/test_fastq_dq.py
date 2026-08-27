"""Tests for the FASTQ data-quality gate."""
from pathlib import Path

import pytest

from theta_warehouse.config import Genomics
from theta_warehouse.dq import DataQualityError
from theta_warehouse.fastq_source import ingest_fastq
from theta_warehouse.fastq_dq import run_fastq_checks


def _genomics(tmp_path: Path, phred_max: int = 45) -> Genomics:
    return Genomics(
        fastq_parquet_root=tmp_path / "genomics_lake",
        phred_offset=33,
        phred_max=phred_max,
        min_reads=1,
        allowed_bases="ACGTN",
    )


def _write_fastq(path: Path, records: list[tuple[str, str]]) -> None:
    lines = []
    for index, (seq, qual) in enumerate(records):
        lines += [f"@read{index}", seq, "+", qual]
    path.write_text("\n".join(lines) + "\n")


def _outcome(outcomes, name):
    return next(o for o in outcomes if o.check.name == name)


def test_clean_lake_passes_all_checks(tmp_path):
    src = tmp_path / "s.fastq"
    _write_fastq(src, [("ACGT", "IIII"), ("GGCC", "IIII")])  # 'I' = Phred 40
    genomics = _genomics(tmp_path)
    ingest_fastq(genomics, [src])
    outcomes = run_fastq_checks(genomics, genomics.fastq_parquet_root)
    assert all(o.passed for o in outcomes)


def test_empty_lake_fails_not_empty(tmp_path):
    genomics = _genomics(tmp_path)
    genomics.fastq_parquet_root.mkdir(parents=True)
    with pytest.raises(DataQualityError, match="fastq_not_empty|FASTQ data-quality"):
        run_fastq_checks(genomics, genomics.fastq_parquet_root)


def test_phred_above_configured_max_fails(tmp_path):
    src = tmp_path / "s.fastq"
    _write_fastq(src, [("ACGT", "IIII")])  # Phred 40 reads
    genomics = _genomics(tmp_path, phred_max=30)  # but ceiling set to 30
    ingest_fastq(genomics, [src])
    outcomes = run_fastq_checks(genomics, genomics.fastq_parquet_root, raise_on_error=False)
    assert not _outcome(outcomes, "phred_in_range").passed


def test_low_quality_warns_but_does_not_block(tmp_path):
    src = tmp_path / "s.fastq"
    _write_fastq(src, [("ACGT", "!!!!")])  # '!' = Phred 0
    genomics = _genomics(tmp_path)
    ingest_fastq(genomics, [src])
    # A warn-severity failure must not raise.
    outcomes = run_fastq_checks(genomics, genomics.fastq_parquet_root)
    warn = _outcome(outcomes, "mean_quality_reasonable")
    assert not warn.passed
    assert warn.check.severity == "warn"
