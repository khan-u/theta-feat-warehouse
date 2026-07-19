"""Tests for nwb_source pure-logic helpers (no NWB file required)."""
from pathlib import Path

import pytest

from theta_warehouse.nwb_source import (
    REGION_CODE_BY_BASE,
    discover_lfp_files,
    _condense_location,
    _subject_and_session,
)


def test_condense_location_known_region():
    code, hemi = _condense_location("hippocampus_right")
    assert code == "Hipp"
    assert hemi == "R"


def test_condense_location_left_hemisphere():
    code, hemi = _condense_location("amygdala_left")
    assert code == "Amg"
    assert hemi == "L"


def test_condense_location_unknown_falls_back_to_alphanumeric():
    code, hemi = _condense_location("unknown_region_right")
    assert code.isalnum()
    assert hemi == "R"


def test_subject_and_session_uses_patient_code():
    subject_id, session_id = _subject_and_session("sub-10_ses-1_P68CS", "10", "1")
    assert subject_id == "P68CS"
    assert session_id == "1"


def test_subject_and_session_falls_back_to_field():
    subject_id, session_id = _subject_and_session("sub-10_ses-1_unnamed", "99", "2")
    assert "99" in subject_id or subject_id == "sub99"


def test_region_code_by_base_has_five_entries():
    assert len(REGION_CODE_BY_BASE) == 5


def test_discover_lfp_files_deduplicates(tmp_path):
    nwb = tmp_path / "test.nwb"
    nwb.touch()
    result = discover_lfp_files([nwb, nwb])
    assert len(result) == 1


def test_discover_lfp_files_recurses_directories(tmp_path):
    sub = tmp_path / "sub01"
    sub.mkdir()
    (sub / "session.nwb").touch()
    result = discover_lfp_files([tmp_path])
    assert len(result) == 1
