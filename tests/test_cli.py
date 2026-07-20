"""Tests for the CLI parser (no filesystem or DuckDB needed)."""
import pytest

from theta_warehouse.cli import build_parser


def _parse(args):
    return build_parser().parse_args(args)


def test_synth_command_defaults():
    args = _parse(["synth"])
    assert args.command == "synth"
    assert args.profile == "demo"
    assert args.effect == 0.0


def test_synth_profile_choices():
    args = _parse(["synth", "--profile", "full"])
    assert args.profile == "full"


def test_run_all_accepts_subjects_filter():
    args = _parse(["run-all", "--subjects-filter", "P01", "P02"])
    assert args.subjects_filter == ["P01", "P02"]


def test_dq_no_fail_flag():
    args = _parse(["dq", "--no-fail"])
    assert args.no_fail is True


def test_nwb_regions_filter():
    args = _parse(["nwb", "file.nwb", "--regions", "Hipp", "Amg"])
    assert args.regions == ["Hipp", "Amg"]


def test_missing_required_command_raises():
    with pytest.raises(SystemExit):
        _parse([])


def test_all_commands_are_registered():
    expected = {"synth", "nwb", "init", "discover", "load", "transform", "dq", "analyze", "export", "run-all"}
    parser = build_parser()
    subactions = {
        a.dest: [c for c in a.choices]
        for a in parser._subparsers._group_actions
        if hasattr(a, "choices")
    }
    registered = set(list(subactions.values())[0])
    assert expected == registered
