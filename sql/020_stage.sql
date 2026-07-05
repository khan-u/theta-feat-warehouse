-- Staging layer: views over the Parquet lake, no data movement.
--
-- Hive-style directory names (subject_id=..., region=..., extraction_id=...)
-- become real columns via hive_partitioning, so a query filtered to one subject
-- reads one directory instead of scanning the lake.

CREATE OR REPLACE VIEW stg.cycle_features AS
SELECT
    subject_id,
    region,
    extraction_id,
    extracted_at,
    trial,
    channel_idx,
    channel_label,
    -- burst-detection criteria
    amp_fraction,
    amp_consistency,
    period_consistency,
    monotonicity,
    -- cycle timing, in samples as bycycle emits them
    period,
    time_peak,
    time_trough,
    time_rise,
    time_decay,
    -- voltage
    volt_peak,
    volt_trough,
    volt_rise,
    volt_decay,
    volt_amp,
    band_amp,
    -- the two waveform-shape measures under test
    time_ptsym,
    time_rdsym,
    -- sample landmarks
    sample_peak,
    sample_last_zerox_decay,
    sample_zerox_decay,
    sample_zerox_rise,
    sample_last_trough,
    sample_next_trough,
    is_burst,
    source_file,
    run_id,
    loaded_at
FROM read_parquet(
    '{{parquet_root}}/**/*.parquet',
    hive_partitioning = true,
    union_by_name = true
);

-- RunBycycle.py writes a new timestamped file per extraction rather than
-- overwriting, so the same (subject, region) can appear several times in the
-- lake. Only the newest extraction per pair is analysed, mirroring the
-- "retain only the latest merged CSV" rule the upstream script applies to its
-- own merged output. Older extractions stay in the lake for lineage.
CREATE OR REPLACE VIEW stg.latest_extraction AS
WITH ranked AS (
    SELECT
        subject_id,
        region,
        extraction_id,
        MAX(extracted_at) AS extracted_at,
        ROW_NUMBER() OVER (
            PARTITION BY subject_id, region
            ORDER BY MAX(extracted_at) DESC, extraction_id DESC
        ) AS recency_rank
    FROM stg.cycle_features
    GROUP BY subject_id, region, extraction_id
)
SELECT subject_id, region, extraction_id, extracted_at
FROM ranked
WHERE recency_rank = 1;
