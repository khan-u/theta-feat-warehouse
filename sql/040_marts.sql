-- Mart layer: the tables the analysis and the dashboard read.

-- Per channel x load summary. This is the "aggregate per Channel x Load" step the
-- upstream README specifies, and the analysis unit is the channel, not the cycle.
--
-- burst_only is a config flag rather than a hard-coded filter: bycycle's
-- asymmetry measures are only meaningful for cycles that are part of an
-- oscillatory burst, and keeping it configurable allows checking that the
-- result does not depend on that choice.
CREATE OR REPLACE TABLE mart.channel_load_asym AS
SELECT
    subject_id,
    region,
    channel_label,
    load_condition,
    COUNT(*)                                        AS n_cycles,
    COUNT(DISTINCT trial)                           AS n_trials,
    AVG(time_ptsym)                                 AS mean_ptsym,
    MEDIAN(time_ptsym)                              AS median_ptsym,
    STDDEV_SAMP(time_ptsym)                         AS sd_ptsym,
    AVG(time_rdsym)                                 AS mean_rdsym,
    MEDIAN(time_rdsym)                              AS median_rdsym,
    STDDEV_SAMP(time_rdsym)                         AS sd_rdsym,
    AVG(cycle_freq_hz)                              AS mean_cycle_freq_hz,
    AVG(volt_amp)                                   AS mean_volt_amp,
    MAX(extracted_at)                               AS extracted_at
FROM core.fct_theta_cycle
WHERE load_condition IS NOT NULL
  AND time_ptsym IS NOT NULL
  AND time_rdsym IS NOT NULL
  AND (NOT {{burst_only}} OR is_burst)
GROUP BY subject_id, region, channel_label, load_condition;

-- Paired table: one row per channel, both conditions side by side.
--
-- Pivoting with conditional aggregates keeps the pairing explicit and lets the
-- inclusion rule be applied symmetrically. A channel is included only if BOTH
-- conditions clear the minimum cycle count; dropping a channel from one
-- condition only would bias the paired difference.
CREATE OR REPLACE TABLE mart.channel_paired_asym AS
WITH pivoted AS (
    SELECT
        subject_id,
        region,
        channel_label,
        MAX(n_cycles)   FILTER (WHERE load_condition = {{baseline_condition}})   AS n_cycles_baseline,
        MAX(n_cycles)   FILTER (WHERE load_condition = {{comparison_condition}}) AS n_cycles_comparison,
        MAX(n_trials)   FILTER (WHERE load_condition = {{baseline_condition}})   AS n_trials_baseline,
        MAX(n_trials)   FILTER (WHERE load_condition = {{comparison_condition}}) AS n_trials_comparison,
        MAX(mean_ptsym) FILTER (WHERE load_condition = {{baseline_condition}})   AS ptsym_baseline,
        MAX(mean_ptsym) FILTER (WHERE load_condition = {{comparison_condition}}) AS ptsym_comparison,
        MAX(mean_rdsym) FILTER (WHERE load_condition = {{baseline_condition}})   AS rdsym_baseline,
        MAX(mean_rdsym) FILTER (WHERE load_condition = {{comparison_condition}}) AS rdsym_comparison,
        MAX(mean_cycle_freq_hz)                                                  AS mean_cycle_freq_hz
    FROM mart.channel_load_asym
    GROUP BY subject_id, region, channel_label
)
SELECT
    subject_id,
    region,
    channel_label,
    n_cycles_baseline,
    n_cycles_comparison,
    n_trials_baseline,
    n_trials_comparison,
    ptsym_baseline,
    ptsym_comparison,
    ptsym_comparison - ptsym_baseline AS ptsym_diff,
    rdsym_baseline,
    rdsym_comparison,
    rdsym_comparison - rdsym_baseline AS rdsym_diff,
    mean_cycle_freq_hz,
    (
        ptsym_baseline    IS NOT NULL
    AND ptsym_comparison  IS NOT NULL
    AND rdsym_baseline    IS NOT NULL
    AND rdsym_comparison  IS NOT NULL
    AND COALESCE(n_cycles_baseline, 0)   >= {{min_cycles_per_channel_load}}
    AND COALESCE(n_cycles_comparison, 0) >= {{min_cycles_per_channel_load}}
    )                                  AS included,
    CASE
        WHEN ptsym_baseline IS NULL OR ptsym_comparison IS NULL
          OR rdsym_baseline IS NULL OR rdsym_comparison IS NULL
            THEN 'missing one condition'
        WHEN COALESCE(n_cycles_baseline, 0)   < {{min_cycles_per_channel_load}}
          OR COALESCE(n_cycles_comparison, 0) < {{min_cycles_per_channel_load}}
            THEN 'below minimum cycle count'
        ELSE NULL
    END                                AS exclusion_reason
FROM pivoted;

-- Distribution of the symmetry metrics for the dashboard, pre-binned so Tableau
-- plots a small aggregate table instead of the full set of cycle rows.
CREATE OR REPLACE TABLE mart.cycle_qc_distribution AS
WITH unpivoted AS (
    SELECT subject_id, region, load_condition, is_burst,
           'time_ptsym' AS metric, time_ptsym AS value
    FROM core.fct_theta_cycle
    WHERE time_ptsym IS NOT NULL
    UNION ALL
    SELECT subject_id, region, load_condition, is_burst,
           'time_rdsym' AS metric, time_rdsym AS value
    FROM core.fct_theta_cycle
    WHERE time_rdsym IS NOT NULL
)
SELECT
    metric,
    region,
    load_condition,
    is_burst,
    FLOOR(value * 50) / 50.0        AS bin_start,
    (FLOOR(value * 50) + 1) / 50.0  AS bin_end,
    COUNT(*)                        AS n_cycles,
    COUNT(DISTINCT subject_id)      AS n_subjects
FROM unpivoted
GROUP BY metric, region, load_condition, is_burst, FLOOR(value * 50);

-- Per-subject coverage, for the dashboard's "who contributed what" view.
CREATE OR REPLACE TABLE mart.subject_coverage AS
SELECT
    f.subject_id,
    f.region,
    COUNT(DISTINCT f.channel_label)                                     AS n_channels,
    COUNT(DISTINCT f.trial)                                             AS n_trials,
    COUNT(*)                                                            AS n_cycles,
    COUNT(*) FILTER (WHERE f.is_burst)                                  AS n_burst_cycles,
    COUNT(*) FILTER (WHERE f.is_burst) * 1.0 / NULLIF(COUNT(*), 0)      AS burst_fraction,
    COUNT(DISTINCT p.channel_label) FILTER (WHERE p.included)           AS n_channels_included,
    MAX(f.extracted_at)                                                 AS extracted_at
FROM core.fct_theta_cycle AS f
LEFT JOIN mart.channel_paired_asym AS p
  ON  f.subject_id    = p.subject_id
  AND f.region        = p.region
  AND f.channel_label = p.channel_label
GROUP BY f.subject_id, f.region;

-- Pipeline health: one row per run, joining file counts and DQ outcomes so the
-- dashboard can show whether the data is trustworthy next to what it says.
CREATE OR REPLACE TABLE mart.run_health AS
WITH files AS (
    SELECT
        run_id,
        COUNT(*)                                        AS files_seen,
        COUNT(*) FILTER (WHERE status = 'loaded')        AS files_loaded,
        COUNT(*) FILTER (WHERE status = 'skipped')       AS files_skipped,
        SUM(COALESCE(row_count, 0))                      AS rows_loaded,
        SUM(COALESCE(size_bytes, 0))                     AS bytes_read
    FROM ops.source_file
    GROUP BY run_id
),
checks AS (
    SELECT
        run_id,
        COUNT(*)                                                    AS checks_run,
        COUNT(*) FILTER (WHERE NOT passed)                          AS checks_failed,
        COUNT(*) FILTER (WHERE NOT passed AND severity = 'error')   AS errors,
        COUNT(*) FILTER (WHERE NOT passed AND severity = 'warn')    AS warnings
    FROM ops.dq_result
    GROUP BY run_id
)
SELECT
    r.run_id,
    r.triggered_by,
    r.started_at,
    r.finished_at,
    DATE_DIFF('second', r.started_at, COALESCE(r.finished_at, r.started_at)) AS duration_s,
    r.status,
    r.message,
    COALESCE(f.files_seen, 0)     AS files_seen,
    COALESCE(f.files_loaded, 0)   AS files_loaded,
    COALESCE(f.files_skipped, 0)  AS files_skipped,
    COALESCE(f.rows_loaded, 0)    AS rows_loaded,
    COALESCE(f.bytes_read, 0)     AS bytes_read,
    COALESCE(c.checks_run, 0)     AS checks_run,
    COALESCE(c.checks_failed, 0)  AS checks_failed,
    COALESCE(c.errors, 0)         AS dq_errors,
    COALESCE(c.warnings, 0)       AS dq_warnings
FROM ops.pipeline_run AS r
LEFT JOIN files  AS f ON r.run_id = f.run_id
LEFT JOIN checks AS c ON r.run_id = c.run_id;
