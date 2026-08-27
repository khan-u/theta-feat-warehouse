"""Tests for fastq_source pure-logic helpers (no file or database required)."""
import pytest

from theta_warehouse.fastq_source import (
    READ_METRICS_COLUMN_NAMES,
    FastqFormatError,
    FastqRecord,
    compute_metrics,
    gc_content,
    mean_phred,
    parse_fastq,
    parse_read_id,
    phred_scores,
    validate_record,
)

# Two four-line records; 'I' is Phred 40 at offset 33, '!' is Phred 0.
TWO_RECORDS = [
    "@read1 first",
    "ACGT",
    "+",
    "IIII",
    "@read2",
    "GGCC",
    "+",
    "!!!!",
]


def test_gc_content_bounds():
    assert gc_content("GGCC") == 1.0
    assert gc_content("ATAT") == 0.0
    assert gc_content("ACGT") == 0.5
    assert gc_content("") == 0.0


def test_phred_scores_sanger_offset():
    assert phred_scores("!I", 33) == [0, 40]


def test_mean_phred_and_empty():
    assert mean_phred("IIII", 33) == 40.0
    assert mean_phred("", 33) == 0.0


def test_parse_read_id_strips_at_and_description():
    assert parse_read_id("@SRR001.1 length=76") == "SRR001.1"
    assert parse_read_id("@bare") == "bare"


def test_parse_fastq_yields_records():
    records = list(parse_fastq(TWO_RECORDS))
    assert len(records) == 2
    assert records[0] == FastqRecord(read_id="read1", sequence="ACGT", quality="IIII")
    assert records[1].read_id == "read2"


def test_parse_fastq_tolerates_trailing_blank_line():
    records = list(parse_fastq(TWO_RECORDS + [""]))
    assert len(records) == 2


def test_parse_fastq_truncated_raises():
    with pytest.raises(FastqFormatError, match="multiple of four"):
        list(parse_fastq(TWO_RECORDS[:6]))


def test_parse_fastq_bad_header_raises():
    with pytest.raises(FastqFormatError, match="'@' header"):
        list(parse_fastq(["read1", "ACGT", "+", "IIII"]))


def test_validate_record_accepts_clean_read():
    assert validate_record(FastqRecord("r", "ACGTN", "IIIII"), "ACGTN") is None


def test_validate_record_flags_length_mismatch():
    reason = validate_record(FastqRecord("r", "ACGT", "III"), "ACGTN")
    assert reason is not None and "length mismatch" in reason


def test_validate_record_flags_invalid_base():
    reason = validate_record(FastqRecord("r", "ACXT", "IIII"), "ACGTN")
    assert reason is not None and "invalid base" in reason


def test_validate_record_flags_empty_sequence():
    assert validate_record(FastqRecord("r", "", ""), "ACGTN") == "empty sequence"


def test_compute_metrics_values():
    metrics = compute_metrics(FastqRecord("r", "GGCAN", "IIII!"), 33)
    assert metrics.seq_length == 5
    assert metrics.gc_content == pytest.approx(3 / 5)  # G, G, C
    assert metrics.n_bases == 1
    assert metrics.min_phred == 0
    assert metrics.max_phred == 40


def test_read_metrics_columns_and_row_alignment():
    metrics = compute_metrics(FastqRecord("r", "ACGT", "IIII"), 33)
    assert len(READ_METRICS_COLUMN_NAMES) == 7
    assert len(metrics.as_row()) == len(READ_METRICS_COLUMN_NAMES)
    assert READ_METRICS_COLUMN_NAMES[0] == "read_id"
