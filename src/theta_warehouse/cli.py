"""This module is the command-line interface.

Every Airflow task maps to one subcommand here, and ``run-all`` chains them in
order. That means the pipeline can be developed, debugged and demonstrated
without an Airflow install, and the DAG stays a thin scheduling wrapper rather
than the only way to execute the logic.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import dq as dq_module
from . import export as export_module
from . import ingest, transform
from . import nwb_source
from .config import Config, load_config
from .db import Warehouse
from .synth import spec_from_profile, generate

LOG = logging.getLogger("theta_warehouse")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def open_warehouse(config: Config, read_only: bool = False) -> Warehouse:
    config.paths.ensure()
    return Warehouse(config.paths.duckdb_path, config.sql_context, read_only=read_only)


# --------------------------------------------------------------------- commands


def cmd_synth(args: argparse.Namespace, config: Config) -> int:
    spec = spec_from_profile(
        args.profile,
        n_subjects=args.subjects,
        n_channels_total=args.channels,
        n_trials=args.trials,
        effect=args.effect,
        seed=args.seed,
        fs=config.signal.fs,
        epoch_start_s=config.signal.epoch_start_s,
        epoch_end_s=config.signal.epoch_end_s,
        min_n_cycles=config.thresholds.min_n_cycles,
    )
    LOG.info(
        "generating synthetic features: %d subjects, %d channels, %d trials, effect=%.4f",
        spec.n_subjects,
        spec.n_channels_total,
        spec.n_trials,
        spec.effect,
    )
    totals = generate(
        spec,
        source_root=config.paths.source_root,
        trial_metadata_root=config.paths.trial_metadata,
        extracted_at=datetime.fromisoformat(args.extracted_at) if args.extracted_at else None,
    )
    LOG.info(
        "wrote %d file(s), %d cycle row(s), %d trial row(s) under %s",
        totals["files"],
        totals["rows"],
        totals["trials"],
        config.paths.source_root,
    )
    return 0


def cmd_nwb(args: argparse.Namespace, config: Config) -> int:
    """Extract cycle features and trial metadata from SBCAT NWB files.

    Reads the spike-removed 400 Hz LFP from each file, epochs it around the
    maintenance period, and runs the same lowpass + bycycle extraction as
    eeg-feat-ext's RunBycycle.py, writing the CSV contract the warehouse loads.
    """
    paths = [Path(entry) for entry in args.paths]
    nwb_files = nwb_source.discover_lfp_files(paths)
    if not nwb_files:
        LOG.error("no .nwb files found in: %s", ", ".join(args.paths))
        return 2

    regions = tuple(args.regions) if args.regions else None
    extracted_at = datetime.fromisoformat(args.extracted_at) if args.extracted_at else None

    processed = 0
    skipped_no_lfp = 0
    for index, nwb_path in enumerate(nwb_files):
        if not nwb_source.file_has_lfp(nwb_path):
            LOG.warning("skipping %s: no LFP ElectricalSeries (spikes-only release)", nwb_path.name)
            skipped_no_lfp += 1
            continue
        LOG.info("extracting %s", nwb_path.name)
        # Stagger the per-file extraction timestamp so each session's feature
        # files carry a distinct extraction_id, as re-runs would upstream.
        file_extracted_at = None
        if extracted_at is not None:
            file_extracted_at = extracted_at + timedelta(minutes=index)
        result = nwb_source.extract_file(
            nwb_path,
            config,
            extracted_at=file_extracted_at,
            regions=regions,
            max_trials=args.max_trials,
        )
        LOG.info("  -> %s", result.summary())
        processed += 1

    LOG.info(
        "processed %d NWB file(s), skipped %d without LFP; features under %s",
        processed,
        skipped_no_lfp,
        config.paths.source_root,
    )
    return 0 if processed > 0 else 1


def cmd_init(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        transform.init_schema(warehouse)
    LOG.info("schema initialised at %s", config.paths.duckdb_path)
    return 0


def cmd_discover(args: argparse.Namespace, config: Config) -> int:
    result = ingest.discover(config, subjects=args.subjects_filter)
    LOG.info("discovery: %s", result.summary)
    for path, reason in result.skipped:
        LOG.warning("skipped %s: %s", Path(path).name, reason)
    return 0


def cmd_load(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        transform.init_schema(warehouse)
        run_id = warehouse.start_run(args.run_id, triggered_by="cli")
        try:
            discovery = ingest.discover(config, subjects=args.subjects_filter)
            ingest.register_source_files(warehouse, run_id, discovery)

            problems = ingest.validate_source_contract(config, discovery)
            if problems:
                for problem in problems:
                    LOG.error("source contract: %s", problem)
                raise RuntimeError(f"{len(problems)} source contract violation(s)")

            totals = ingest.load_to_lake(warehouse, config, run_id, discovery)
            trial_rows = ingest.load_trial_metadata(warehouse, config, run_id)
            LOG.info(
                "loaded %d partition(s), %d cycle row(s), %d trial metadata row(s)",
                totals["partitions"],
                totals["rows"],
                trial_rows,
            )
        except Exception as exc:
            warehouse.finish_run(run_id, "failed", str(exc))
            raise
        warehouse.finish_run(run_id, "success")
    return 0


def cmd_transform(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        transform.build_staging(warehouse)
        core = transform.build_core(warehouse)
        LOG.info("core: %s", core)
        marts = transform.build_marts(warehouse)
        LOG.info("marts: %s", marts)
    return 0


def cmd_dq(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        run_id = args.run_id or _latest_run_id(warehouse)
        outcomes = dq_module.run_checks(
            warehouse, config, run_id, raise_on_error=not args.no_fail
        )
        print(dq_module.format_outcomes(outcomes))
    return 0


def cmd_analyze(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        run_id = args.run_id or _latest_run_id(warehouse)
        results = transform.run_analysis(warehouse, config, run_id)
        print(transform.format_results(results))
    return 0


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    with open_warehouse(config) as warehouse:
        run_id = args.run_id or _latest_run_id(warehouse)
        counts = export_module.export_extracts(warehouse, config, run_id)
    LOG.info("exported extracts to %s: %s", config.paths.export_dir, counts)
    return 0


def cmd_run_all(args: argparse.Namespace, config: Config) -> int:
    """Full pipeline in one process, in the same order as the DAG."""
    with open_warehouse(config) as warehouse:
        transform.init_schema(warehouse)
        run_id = warehouse.start_run(args.run_id, triggered_by="cli:run-all")
        try:
            discovery = ingest.discover(config, subjects=args.subjects_filter)
            ingest.register_source_files(warehouse, run_id, discovery)

            problems = ingest.validate_source_contract(config, discovery)
            if problems:
                for problem in problems:
                    LOG.error("source contract: %s", problem)
                raise RuntimeError(f"{len(problems)} source contract violation(s)")

            totals = ingest.load_to_lake(warehouse, config, run_id, discovery)
            trial_rows = ingest.load_trial_metadata(warehouse, config, run_id)
            LOG.info("load: %s, trial rows: %d", totals, trial_rows)

            transform.build_staging(warehouse)
            LOG.info("core: %s", transform.build_core(warehouse))
            LOG.info("marts: %s", transform.build_marts(warehouse))

            outcomes = dq_module.run_checks(warehouse, config, run_id)
            print(dq_module.format_outcomes(outcomes))

            results = transform.run_analysis(warehouse, config, run_id)
            print(transform.format_results(results))

            counts = export_module.export_extracts(warehouse, config, run_id)
            LOG.info("exports: %s", counts)
        except Exception as exc:
            warehouse.finish_run(run_id, "failed", str(exc))
            raise
        warehouse.finish_run(run_id, "success")
        # Rebuild run_health so the dashboard sees the completed run, not the
        # in-flight snapshot taken before finish_run.
        transform.build_marts(warehouse)
        export_module.export_extracts(warehouse, config, run_id)
    return 0


def _latest_run_id(warehouse: Warehouse) -> str:
    run_id = warehouse.scalar(
        "SELECT run_id FROM ops.pipeline_run ORDER BY started_at DESC LIMIT 1"
    )
    if run_id is None:
        raise RuntimeError("no pipeline runs recorded; run `load` or `run-all` first")
    return str(run_id)


# ----------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="theta-warehouse",
        description="ELT warehouse and analysis for eeg-feat-ext cycle features.",
    )
    parser.add_argument("--config", default="config/pipeline.yml", help="path to pipeline.yml")
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler, help_text: str) -> argparse.ArgumentParser:
        sub = subparsers.add_parser(name, help=help_text)
        sub.set_defaults(handler=handler)
        return sub

    synth = add("synth", cmd_synth, "generate synthetic cycle-feature CSVs")
    synth.add_argument("--profile", choices=("demo", "full"), default="demo")
    synth.add_argument("--subjects", type=int, default=None, help="override subject count")
    synth.add_argument("--channels", type=int, default=None, help="override total channel count")
    synth.add_argument("--trials", type=int, default=None, help="override trials per subject")
    synth.add_argument(
        "--effect",
        type=float,
        default=0.0,
        help="inject a load-dependent symmetry shift (0.0 = null is true)",
    )
    synth.add_argument("--seed", type=int, default=None)
    synth.add_argument("--extracted-at", default=None, help="ISO timestamp for filenames")

    nwb = add("nwb", cmd_nwb, "extract cycle features from SBCAT NWB LFP files")
    nwb.add_argument("paths", nargs="+", help="NWB files or directories to ingest")
    nwb.add_argument(
        "--regions",
        nargs="*",
        default=None,
        help="restrict to these condensed region codes (e.g. Hipp Amg dACC preSMA vmPFC)",
    )
    nwb.add_argument("--max-trials", type=int, default=None, help="cap trials per file (for a quick run)")
    nwb.add_argument("--extracted-at", default=None, help="ISO timestamp base for feature filenames")

    add("init", cmd_init, "create schemas and operational tables")

    discover = add("discover", cmd_discover, "list source files without loading")
    discover.add_argument("--subjects-filter", nargs="*", default=None)

    load = add("load", cmd_load, "validate and load CSVs into the Parquet lake")
    load.add_argument("--subjects-filter", nargs="*", default=None)
    load.add_argument("--run-id", default=None)

    add("transform", cmd_transform, "build staging views, core fact and marts")

    dq_parser = add("dq", cmd_dq, "run data-quality checks")
    dq_parser.add_argument("--run-id", default=None)
    dq_parser.add_argument(
        "--no-fail", action="store_true", help="record failures without a non-zero exit"
    )

    analyze = add("analyze", cmd_analyze, "run permutation tests and persist results")
    analyze.add_argument("--run-id", default=None)

    export_parser = add("export", cmd_export, "write Tableau CSV extracts")
    export_parser.add_argument("--run-id", default=None)

    run_all = add("run-all", cmd_run_all, "load, transform, check, analyse and export")
    run_all.add_argument("--subjects-filter", nargs="*", default=None)
    run_all.add_argument("--run-id", default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    try:
        config = load_config(args.config)
    except Exception as exc:
        LOG.error("failed to load config: %s", exc)
        return 2

    try:
        return int(args.handler(args, config))
    except dq_module.DataQualityError as exc:
        LOG.error("%s", exc)
        return 3
    except Exception as exc:
        LOG.error("%s: %s", type(exc).__name__, exc)
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
