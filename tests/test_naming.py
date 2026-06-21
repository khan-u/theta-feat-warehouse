"""This module tests the eeg-feat-ext file naming contract."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from theta_warehouse.naming import (
    NamingError,
    format_feature_filename,
    is_merged_file,
    parse_feature_filename,
    parse_source_path,
)


def test_parses_region_feature_filename():
    subject, region, extracted_at = parse_feature_filename(
        "P60cs_HP_bycycle_features_20250601_091500.csv"
    )
    assert subject == "P60cs"
    assert region == "HP"
    assert extracted_at == datetime(2025, 6, 1, 9, 15, 0)


def test_subject_id_may_contain_underscores():
    subject, region, _ = parse_feature_filename(
        "P_60_cs_SMA_bycycle_features_20250601_091500.csv"
    )
    assert subject == "P_60_cs"
    assert region == "SMA"


def test_merged_session_file_is_recognised_and_rejected():
    name = "P60cs_merged_bycycle_features_20250601_091500.csv"
    assert is_merged_file(name)
    with pytest.raises(NamingError):
        parse_feature_filename(name)


def test_repository_sample_placeholder_is_rejected():
    # The eeg-feat-ext repo ships this file with dtype names in place of values.
    with pytest.raises(NamingError):
        parse_feature_filename(
            "SubjectID1_BrainRegion2_bycycle_features_YYYYMMDD_#####_sample.csv"
        )


def test_roundtrip_format_and_parse():
    stamp = datetime(2026, 12, 1, 23, 59, 59)
    name = format_feature_filename("P77cs", "AC", stamp)
    assert parse_feature_filename(name) == ("P77cs", "AC", stamp)


def test_path_and_filename_must_agree(tmp_path: Path):
    root = tmp_path / "cycle_features"
    good = root / "P60cs" / "HP"
    good.mkdir(parents=True)
    path = good / "P60cs_HP_bycycle_features_20250601_091500.csv"
    path.touch()

    source = parse_source_path(path, root)
    assert source.subject_id == "P60cs"
    assert source.region == "HP"
    assert source.extraction_id == "20250601_091500"


def test_misfiled_path_raises(tmp_path: Path):
    root = tmp_path / "cycle_features"
    wrong = root / "P60cs" / "A"  # filename says HP, directory says A
    wrong.mkdir(parents=True)
    path = wrong / "P60cs_HP_bycycle_features_20250601_091500.csv"
    path.touch()

    with pytest.raises(NamingError, match="region mismatch"):
        parse_source_path(path, root)
