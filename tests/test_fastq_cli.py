"""End-to-end tests for the `fastq` CLI subcommand."""
import textwrap
from pathlib import Path

from theta_warehouse.cli import main

FASTQ = "\n".join(["@r1", "ACGT", "+", "IIII", "@r2", "GGCC", "+", "IIII"]) + "\n"


def _write_config(tmp_path: Path, phred_max: int = 45) -> Path:
    body = textwrap.dedent(
        f"""\
        paths:
          source_root: data/cycle_features
          trial_metadata: data/trial_metadata
          warehouse_dir: warehouse
          parquet_root: warehouse/lake
          duckdb_path: warehouse/theta.duckdb
          export_dir: warehouse/exports
        signal:
          fs: 400
          f_theta: [3, 7]
          f_lowpass: 30
          epoch_window_s: [-0.3, 2.8]
        bycycle_thresholds:
          amp_fraction_threshold: 0.2
          amp_consistency_threshold: 0.1
          period_consistency_threshold: 0.4
          monotonicity_threshold: 0.4
          min_n_cycles: 3
        analysis:
          burst_only: true
          metrics: [time_ptsym, time_rdsym]
          conditions:
            baseline: 1
            comparison: 3
          min_cycles_per_channel_load: 10
          n_permutations: 10000
          random_seed: 42
          symmetry_null_value: 0.5
        dq:
          max_null_fraction: 0.05
          max_channel_dropout_fraction: 0.10
          symmetry_bounds: [0.0, 1.0]
          min_rows_per_file: 1
        genomics:
          fastq_parquet_root: warehouse/genomics_lake
          phred_offset: 33
          phred_max: {phred_max}
          min_reads: 1
          allowed_bases: ACGTN
        """
    )
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg_file = cfg_dir / "pipeline.yml"
    cfg_file.write_text(body)
    return cfg_file


def test_fastq_cli_happy_path(tmp_path):
    cfg = _write_config(tmp_path)
    fastq = tmp_path / "s.fastq"
    fastq.write_text(FASTQ)

    code = main(["--config", str(cfg), "fastq", str(fastq), "--sample", "demo"])
    assert code == 0

    parquet = tmp_path / "warehouse" / "genomics_lake" / "sample_id=demo" / "part-0.parquet"
    assert parquet.exists()


def test_fastq_cli_exits_3_on_quality_failure(tmp_path):
    # Reads are Phred 40 but the ceiling is set to 30, so phred_in_range fails.
    cfg = _write_config(tmp_path, phred_max=30)
    fastq = tmp_path / "s.fastq"
    fastq.write_text(FASTQ)

    code = main(["--config", str(cfg), "fastq", str(fastq)])
    assert code == 3


def test_fastq_cli_missing_file_exits_2(tmp_path):
    cfg = _write_config(tmp_path)
    code = main(["--config", str(cfg), "fastq", str(tmp_path / "nope.fastq")])
    assert code == 2
