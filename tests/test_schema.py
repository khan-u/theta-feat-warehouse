"""This module tests the column contract, including the BOM the real files carry."""

from __future__ import annotations

from theta_warehouse.schema import (
    SOURCE_COLUMN_ORDER,
    SYMMETRY_METRICS,
    duckdb_columns_struct,
    missing_columns,
    unexpected_columns,
)


def test_contract_has_the_27_columns_runbycycle_writes():
    # 3 identity columns inserted by RunBycycle.py + 24 bycycle feature columns.
    assert len(SOURCE_COLUMN_ORDER) == 27
    assert SOURCE_COLUMN_ORDER[:3] == ("trial", "channel_idx", "channel_label")
    assert SOURCE_COLUMN_ORDER[-1] == "is_burst"


def test_symmetry_metrics_are_present_in_the_contract():
    for metric in SYMMETRY_METRICS:
        assert metric in SOURCE_COLUMN_ORDER


def test_bom_prefixed_header_is_tolerated():
    header = list(SOURCE_COLUMN_ORDER)
    header[0] = "\ufefftrial"  # what a Windows-written CSV actually delivers
    assert missing_columns(header) == []
    assert unexpected_columns(header) == []


def test_missing_column_is_reported():
    header = [c for c in SOURCE_COLUMN_ORDER if c != "time_ptsym"]
    assert missing_columns(header) == ["time_ptsym"]


def test_unexpected_column_is_reported():
    header = list(SOURCE_COLUMN_ORDER) + ["theta_power"]
    assert unexpected_columns(header) == ["theta_power"]


def test_duckdb_struct_literal_is_wellformed():
    literal = duckdb_columns_struct()
    assert literal.startswith("{") and literal.endswith("}")
    assert "'time_ptsym': 'DOUBLE'" in literal
    assert "'is_burst': 'BOOLEAN'" in literal
