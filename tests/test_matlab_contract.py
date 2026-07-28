"""Verify the MATLAB export script satisfies the trial metadata contract."""
from pathlib import Path


MATLAB_SCRIPT = Path(__file__).parent.parent / "matlab" / "export_trialinfo.m"


def test_script_exists():
    assert MATLAB_SCRIPT.is_file()


def test_output_header_matches_warehouse_schema():
    source = MATLAB_SCRIPT.read_text(encoding="utf-8")
    assert "subject_id,trial,load_condition,correct" in source


def test_uses_zero_based_trial_index():
    source = MATLAB_SCRIPT.read_text(encoding="utf-8")
    assert "0:nTrials" in source or "(0:" in source


def test_documents_the_off_by_one_risk():
    source = MATLAB_SCRIPT.read_text(encoding="utf-8")
    assert "0-based" in source or "zero-based" in source.lower()
