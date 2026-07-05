-- Core layer: one typed fact table at cycle grain, plus its dimensions.

-- Grain: one row per (subject, region, channel, trial, cycle).
--
-- cycle_idx has to be reconstructed. RunBycycle.py writes with index=False, so
-- bycycle's cycle number is discarded on export; sample_last_trough is
-- monotonically increasing within a (trial, channel) signal, which makes it a
-- deterministic ordering key. Recomputing the index here rather than trusting
-- file order means the result does not depend on how Parquet returns rows.
CREATE OR REPLACE TABLE core.fct_theta_cycle AS
WITH latest AS (
    SELECT c.*
    FROM stg.cycle_features AS c
    JOIN stg.latest_extraction AS l
      ON  c.subject_id    = l.subject_id
      AND c.region        = l.region
      AND c.extraction_id = l.extraction_id
),
indexed AS (
    SELECT
        latest.*,
        ROW_NUMBER() OVER (
            PARTITION BY subject_id, region, channel_label, trial
            ORDER BY sample_last_trough, sample_peak
        ) AS cycle_idx,
        COUNT(*) OVER (
            PARTITION BY subject_id, region, channel_label, trial
        ) AS cycles_in_signal
    FROM latest
)
SELECT
    i.subject_id,
    i.region,
    i.channel_label,
    i.channel_idx,
    i.trial,
    i.cycle_idx,
    i.cycles_in_signal,
    t.load_condition,
    t.correct                                        AS trial_correct,
    i.is_burst,
    i.time_ptsym,
    i.time_rdsym,
    i.period,
    i.period / {{fs}}.0                              AS period_s,
    CASE WHEN i.period > 0 THEN {{fs}}.0 / i.period END AS cycle_freq_hz,
    i.volt_amp,
    i.band_amp,
    i.amp_fraction,
    i.amp_consistency,
    i.period_consistency,
    i.monotonicity,
    i.sample_last_trough,
    i.sample_next_trough,
    i.extraction_id,
    i.extracted_at,
    i.source_file,
    i.run_id,
    i.loaded_at
FROM indexed AS i
LEFT JOIN core.trial_metadata AS t
  ON  i.subject_id = t.subject_id
  AND i.trial      = t.trial;

-- Channel dimension: coverage per channel, used by the dropout check and as the
-- denominator for "how many channels survived filtering".
CREATE OR REPLACE TABLE core.dim_channel AS
SELECT
    subject_id,
    region,
    channel_label,
    MIN(channel_idx)                                        AS channel_idx,
    COUNT(*)                                                AS n_cycles,
    COUNT(*) FILTER (WHERE is_burst)                         AS n_burst_cycles,
    COUNT(DISTINCT trial)                                    AS n_trials,
    COUNT(DISTINCT load_condition)                           AS n_conditions,
    AVG(cycle_freq_hz)                                       AS mean_cycle_freq_hz,
    SUM(CASE WHEN time_ptsym IS NULL THEN 1 ELSE 0 END)      AS n_null_ptsym,
    MAX(extracted_at)                                        AS extracted_at
FROM core.fct_theta_cycle
GROUP BY subject_id, region, channel_label;

-- Trial dimension: condition labels joined to observed coverage, so a trial
-- present in the features but absent from the metadata export is visible.
CREATE OR REPLACE TABLE core.dim_trial AS
SELECT
    f.subject_id,
    f.trial,
    ANY_VALUE(f.load_condition)                AS load_condition,
    ANY_VALUE(f.trial_correct)                 AS trial_correct,
    COUNT(DISTINCT f.channel_label)            AS n_channels,
    COUNT(*)                                   AS n_cycles,
    COUNT(*) FILTER (WHERE f.is_burst)          AS n_burst_cycles,
    BOOL_OR(f.load_condition IS NULL)          AS missing_condition
FROM core.fct_theta_cycle AS f
GROUP BY f.subject_id, f.trial;
