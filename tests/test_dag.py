"""Structural tests for the Airflow DAG (inspects source without executing it)."""
import ast
from pathlib import Path


DAG_PATH = Path(__file__).parent.parent / "dags" / "theta_warehouse_dag.py"


def _dag_ast():
    return ast.parse(DAG_PATH.read_text(encoding="utf-8"))


def test_dag_file_is_valid_python():
    tree = _dag_ast()
    assert isinstance(tree, ast.Module)


def test_dag_defines_default_args():
    source = DAG_PATH.read_text(encoding="utf-8")
    assert "DEFAULT_ARGS" in source
    assert "retries" in source


def test_dag_task_names_in_source():
    source = DAG_PATH.read_text(encoding="utf-8")
    expected_tasks = [
        "extract_nwb",
        "discover",
        "validate_contract",
        "load_lake",
        "load_trial_metadata",
        "build_core",
        "data_quality_gate",
        "build_marts",
        "run_analysis",
        "export_extracts",
        "finalise_run",
    ]
    for task_name in expected_tasks:
        assert task_name in source, f"task {task_name!r} missing from DAG source"


def test_dag_schedule_is_daily():
    source = DAG_PATH.read_text(encoding="utf-8")
    assert '"0 6 * * *"' in source or "'0 6 * * *'" in source
