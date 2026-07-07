"""Tests for the discovery and pre-load validation logic in ingest."""
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from theta_warehouse.ingest import DiscoveryResult, discover, validate_source_contract
from theta_warehouse.naming import SourceFile
from theta_warehouse.schema import SOURCE_COLUMNS


def _make_source_file(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def _minimal_csv_header() -> str:
    return ",".join(SOURCE_COLUMNS) + "\n"


def _make_config(source_root: Path, min_rows: int = 1) -> MagicMock:
    cfg = MagicMock()
    cfg.paths.source_root = source_root
    cfg.dq.min_rows_per_file = min_rows
    return cfg


def _make_source(path: Path, subject: str = "sub01", region: str = "HPC") -> SourceFile:
    from datetime import datetime, timezone
    return SourceFile(
        path=path,
        subject_id=subject,
        region=region,
        extracted_at=datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc),
    )


def test_discover_skips_merged_files(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    sub = root / "sub01" / "HPC"
    sub.mkdir(parents=True)
    (sub / "sub01_HPC_bycycle_merged.csv").write_text("header\n")
    (sub / "sub01_HPC_bycycle_features_20240601_120000.csv").write_text("header\n")

    cfg = _make_config(root)
    result = discover(cfg)

    assert any("merged" in reason for _, reason in result.skipped)


def test_discover_raises_when_root_missing(tmp_path):
    cfg = _make_config(tmp_path / "nonexistent")
    with pytest.raises(FileNotFoundError):
        discover(cfg)


def test_validate_source_contract_detects_missing_columns(tmp_path):
    bad_csv = tmp_path / "bad.csv"
    bad_csv.write_text("trial,channel_idx\ndata\n", encoding="utf-8")
    source = _make_source(bad_csv)
    discovery = DiscoveryResult(files=[source], skipped=[])
    cfg = _make_config(tmp_path)
    problems = validate_source_contract(cfg, discovery)
    assert any("missing columns" in p for p in problems)


def test_validate_source_contract_ok_on_valid_header(tmp_path):
    good_csv = tmp_path / "sub01_HPC_bycycle_features_20240601_120000.csv"
    row = ",".join(["1"] * len(SOURCE_COLUMNS))
    good_csv.write_text(_minimal_csv_header() + row + "\n", encoding="utf-8")
    source = _make_source(good_csv)
    discovery = DiscoveryResult(files=[source], skipped=[])
    cfg = _make_config(tmp_path, min_rows=1)
    problems = validate_source_contract(cfg, discovery)
    assert problems == []
