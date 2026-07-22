"""This module defines the Airflow DAG for the theta cycle-feature warehouse.

Task graph::

    extract_nwb -> discover -> validate_contract -> load_lake -> load_trial_metadata
                -> build_core -> data_quality_gate -> build_marts
                -> run_analysis -> export_extracts -> finalise_run

Design notes:

* **The DAG handles scheduling only.** Every task calls into
  ``theta_warehouse``, so the same code runs from the CLI. This lets the
  pipeline run without an Airflow install and keeps the task functions short.

* **Feature extraction is optional and lives in the DAG.** ``extract_nwb`` runs
  the NWB->CSV bridge (the same bycycle extraction as the ``nwb`` CLI command)
  when ``nwb_source_dir`` is set; otherwise it is a no-op and the DAG runs on the
  CSVs already present (synthetic or previously extracted).

* **The quality gate runs before the marts.** Failing after publishing would
  leave the dashboard showing numbers the pipeline had already flagged as wrong.

* **Tasks are idempotent, so retries and backfill are safe.** Loading replaces
  whole (subject, region, extraction) partitions; core, marts and exports are
  full rebuilds; and ``extract_nwb`` keys the extraction_id to the run's logical
  date. Re-running a task or backfilling a past date rewrites the same state
  rather than duplicating rows, which is what makes ``retries=2`` safe.

* **Airflow's run_id is the warehouse run_id**, so any row in ops.* or any CSV
  extract can be traced back to the DAG run that wrote it.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pendulum
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException

# Set THETA_WAREHOUSE_CONFIG in the Airflow environment to point at pipeline.yml.
CONFIG_PATH = os.environ.get("THETA_WAREHOUSE_CONFIG", "config/pipeline.yml")

DEFAULT_ARGS = {
    "owner": "umais",
    "retries": 2,
    "retry_delay": timedelta(minutes=5),
    "retry_exponential_backoff": True,
    "max_retry_delay": timedelta(minutes=30),
    "execution_timeout": timedelta(hours=2),
}


def _open(read_only: bool = False):
    """Open config and warehouse inside a task.

    Imports are deferred into the task body so DAG parsing stays fast and a
    missing analysis dependency cannot break the scheduler's ability to see
    the DAG at all.
    """
    from theta_warehouse.config import load_config
    from theta_warehouse.db import Warehouse

    config = load_config(CONFIG_PATH)
    config.paths.ensure()
    warehouse = Warehouse(config.paths.duckdb_path, config.sql_context, read_only=read_only)
    return config, warehouse


@dag(
    dag_id="theta_feature_warehouse",
    description="ELT and waveform-shape analysis over eeg-feat-ext cycle features",
    schedule="0 6 * * *",
    start_date=pendulum.datetime(2026, 7, 1, tz="UTC"),
    catchup=False,
    max_active_runs=1,  # the warehouse is a single DuckDB file; no concurrent writers
    default_args=DEFAULT_ARGS,
    tags=["ieeg", "elt", "duckdb", "tableau"],
    params={
        "subjects": [],  # empty = all discovered subjects
        "fail_on_dq_warning": False,
        "nwb_source_dir": "",  # set to a folder of SBCAT NWB files to extract features in-DAG
    },
    doc_md=__doc__,
)
def theta_feature_warehouse():
    @task
    def extract_nwb(**context: Any) -> dict[str, Any]:
        """Extract cycle features from SBCAT NWB LFP files, when configured.

        Runs the same bycycle extraction as the ``nwb`` CLI command. It is a
        no-op unless ``nwb_source_dir`` (param) or THETA_WAREHOUSE_NWB_DIR (env)
        points at a folder of NWB files, so the synthetic and pre-extracted
        paths are unaffected. The extraction_id is derived from the run's logical
        date, so a retry or a backfill of the same date rewrites the same
        partitions rather than accumulating duplicates.
        """
        from theta_warehouse import nwb_source
        from theta_warehouse.config import load_config

        params = context["params"]
        nwb_dir = params.get("nwb_source_dir") or os.environ.get("THETA_WAREHOUSE_NWB_DIR", "")
        if not nwb_dir:
            return {"extracted": 0, "note": "no nwb_source_dir configured; using existing CSVs"}

        config = load_config(CONFIG_PATH)
        config.paths.ensure()
        files = nwb_source.discover_lfp_files([Path(nwb_dir)])

        logical = context["logical_date"]
        base = datetime(logical.year, logical.month, logical.day, 9, 0, 0)

        extracted = 0
        for nwb_path in files:
            if not nwb_source.file_has_lfp(nwb_path):
                continue
            nwb_source.extract_file(
                nwb_path,
                config,
                extracted_at=base + timedelta(minutes=extracted),
            )
            extracted += 1

        if extracted == 0:
            raise AirflowFailException(
                f"nwb_source_dir set to {nwb_dir} but no NWB files with LFP were found"
            )
        return {"extracted": extracted, "source_dir": str(nwb_dir)}

    @task
    def discover(**context: Any) -> dict[str, Any]:
        """Find region-level cycle-feature CSVs and register them for lineage."""
        from theta_warehouse import ingest, transform

        config, warehouse = _open()
        try:
            transform.init_schema(warehouse)
            run_id = warehouse.start_run(context["run_id"], triggered_by="airflow")

            subjects = context["params"].get("subjects") or None
            result = ingest.discover(config, subjects=subjects)
            ingest.register_source_files(warehouse, run_id, result)

            if not result.files:
                raise AirflowFailException(
                    f"no cycle-feature CSVs under {config.paths.source_root}"
                )

            return {
                "run_id": run_id,
                "summary": result.summary,
                "paths": [str(f.path) for f in result.files],
            }
        finally:
            warehouse.close()

    @task
    def validate_contract(discovered: dict[str, Any]) -> dict[str, Any]:
        """Header and row-count checks on the raw files, before anything is written."""
        from theta_warehouse import ingest

        config, warehouse = _open()
        try:
            subjects = None
            result = ingest.discover(config, subjects=subjects)
            problems = ingest.validate_source_contract(config, result)
            if problems:
                raise AirflowFailException(
                    "source contract violations:\n" + "\n".join(f"  - {p}" for p in problems)
                )
            return discovered
        finally:
            warehouse.close()

    @task
    def load_lake(validated: dict[str, Any]) -> dict[str, Any]:
        """Convert each CSV to a sorted, partitioned Parquet file."""
        from theta_warehouse import ingest

        config, warehouse = _open()
        try:
            run_id = validated["run_id"]
            result = ingest.discover(config)
            totals = ingest.load_to_lake(warehouse, config, run_id, result)
            return {**validated, "load": totals}
        finally:
            warehouse.close()

    @task
    def load_trial_metadata(loaded: dict[str, Any]) -> dict[str, Any]:
        """Load the trial-level load conditions the features do not carry."""
        from theta_warehouse import ingest

        config, warehouse = _open()
        try:
            rows = ingest.load_trial_metadata(warehouse, config, loaded["run_id"])
            if rows == 0:
                raise AirflowFailException("trial metadata is empty; the paired analysis needs it")
            return {**loaded, "trial_metadata_rows": rows}
        finally:
            warehouse.close()

    @task
    def build_core(loaded: dict[str, Any]) -> dict[str, Any]:
        """Rebuild staging views, the cycle-grain fact table and its dimensions."""
        from theta_warehouse import transform

        config, warehouse = _open()
        try:
            transform.build_staging(warehouse)
            core = transform.build_core(warehouse)
            if core["fact_rows"] == 0:
                raise AirflowFailException("fact table is empty after core build")
            return {**loaded, "core": core}
        finally:
            warehouse.close()

    @task
    def data_quality_gate(core: dict[str, Any], **context: Any) -> dict[str, Any]:
        """Run the check suite; error-severity failures stop the run here."""
        from theta_warehouse import dq as dq_module
        from theta_warehouse import transform

        config, warehouse = _open()
        try:
            # Marts are needed by the channel-dropout and paired-units checks, so
            # build them first, then gate before analysis and export.
            transform.build_marts(warehouse)
            outcomes = dq_module.run_checks(
                warehouse, config, core["run_id"], raise_on_error=False
            )
            print(dq_module.format_outcomes(outcomes))

            failures = [o for o in outcomes if not o.passed and o.check.severity == "error"]
            warnings = [o for o in outcomes if not o.passed and o.check.severity == "warn"]

            if failures:
                detail = "\n".join(
                    f"  - {o.check.name}: observed={o.observed} "
                    f"{o.check.comparison} {o.check.threshold}. {o.check.detail}"
                    for o in failures
                )
                raise AirflowFailException(f"data-quality gate failed:\n{detail}")

            if warnings and context["params"].get("fail_on_dq_warning"):
                raise AirflowFailException(
                    f"{len(warnings)} data-quality warning(s) with fail_on_dq_warning set"
                )

            return {
                **core,
                "dq": {"checks": len(outcomes), "warnings": len(warnings)},
            }
        finally:
            warehouse.close()

    @task
    def build_marts(gated: dict[str, Any]) -> dict[str, Any]:
        """Rebuild the marts now that the fact table has passed the gate."""
        from theta_warehouse import transform

        config, warehouse = _open()
        try:
            marts = transform.build_marts(warehouse)
            return {**gated, "marts": marts}
        finally:
            warehouse.close()

    @task
    def run_analysis(marts: dict[str, Any]) -> dict[str, Any]:
        """Paired and one-sample permutation tests over the channel-level marts."""
        from theta_warehouse import transform

        config, warehouse = _open()
        try:
            results = transform.run_analysis(warehouse, config, marts["run_id"])
            print(transform.format_results(results))
            return {
                **marts,
                "tests": [
                    {
                        "metric": r.metric,
                        "test": r.test,
                        "n_units": r.n_units,
                        "p_value": r.p_value,
                        "p_value_adjusted": r.p_value_adjusted,
                        "effect_size_dz": r.effect_size_dz,
                    }
                    for r in results
                ],
            }
        finally:
            warehouse.close()

    @task
    def export_extracts(analysed: dict[str, Any]) -> dict[str, Any]:
        """Write the CSV extracts Tableau reads."""
        from theta_warehouse import export as export_module

        config, warehouse = _open()
        try:
            counts = export_module.export_extracts(warehouse, config, analysed["run_id"])
            return {**analysed, "exports": counts}
        finally:
            warehouse.close()

    @task
    def finalise_run(exported: dict[str, Any]) -> dict[str, Any]:
        """Close the run row, then refresh run_health and its extract.

        Ordering matters: run_health reads ops.pipeline_run, so it has to be
        rebuilt after the run is marked successful or the dashboard would show
        every run as perpetually in flight.
        """
        from theta_warehouse import export as export_module
        from theta_warehouse import transform

        config, warehouse = _open()
        try:
            run_id = exported["run_id"]
            warehouse.finish_run(run_id, "success")
            transform.build_marts(warehouse)
            export_module.export_extracts(warehouse, config, run_id)
            return exported
        finally:
            warehouse.close()

    extracted = extract_nwb()
    discovered = discover()
    extracted >> discovered
    validated = validate_contract(discovered)
    loaded = load_lake(validated)
    with_metadata = load_trial_metadata(loaded)
    core = build_core(with_metadata)
    gated = data_quality_gate(core)
    marts = build_marts(gated)
    analysed = run_analysis(marts)
    exported = export_extracts(analysed)
    finalise_run(exported)


dag_instance = theta_feature_warehouse()
