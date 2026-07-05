-- Operational metadata and reference tables.
-- Run first and on every run: all statements are idempotent.

CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS mart;
CREATE SCHEMA IF NOT EXISTS ops;

-- One row per pipeline execution. run_id is the Airflow run_id when the DAG
-- drives the pipeline, so any warehouse row can be traced to the run that wrote it.
CREATE TABLE IF NOT EXISTS ops.pipeline_run (
    run_id       VARCHAR PRIMARY KEY,
    triggered_by VARCHAR NOT NULL,
    started_at   TIMESTAMP NOT NULL,
    finished_at  TIMESTAMP,
    status       VARCHAR NOT NULL,   -- running | success | failed
    message      VARCHAR
);

-- Every file the discovery step considered, loaded or not, so that a file which
-- should have been ingested but was skipped can be seen in the run record.
CREATE TABLE IF NOT EXISTS ops.source_file (
    run_id        VARCHAR NOT NULL,
    source_path   VARCHAR NOT NULL,
    subject_id    VARCHAR,
    region        VARCHAR,
    extraction_id VARCHAR,
    extracted_at  TIMESTAMP,
    size_bytes    BIGINT,
    row_count     BIGINT,
    status        VARCHAR NOT NULL,  -- discovered | loaded | skipped
    reason        VARCHAR,
    seen_at       TIMESTAMP NOT NULL
);

-- Data-quality check outcomes. Stored rather than only logged so the dashboard
-- can show quality over time instead of just the current state.
CREATE SEQUENCE IF NOT EXISTS ops.dq_result_seq START 1;

CREATE TABLE IF NOT EXISTS ops.dq_result (
    dq_id       BIGINT PRIMARY KEY DEFAULT nextval('ops.dq_result_seq'),
    run_id      VARCHAR NOT NULL,
    check_name  VARCHAR NOT NULL,
    severity    VARCHAR NOT NULL,   -- error | warn
    passed      BOOLEAN NOT NULL,
    observed    DOUBLE,
    threshold   DOUBLE,
    detail      VARCHAR,
    checked_at  TIMESTAMP NOT NULL
);

-- Statistical test results, one row per metric per test.
CREATE TABLE IF NOT EXISTS ops.test_result (
    run_id           VARCHAR NOT NULL,
    metric           VARCHAR NOT NULL,
    test             VARCHAR NOT NULL,
    statistic        VARCHAR NOT NULL,
    n_units          INTEGER NOT NULL,
    observed         DOUBLE NOT NULL,
    p_value          DOUBLE NOT NULL,
    p_value_adjusted DOUBLE,
    adjustment       VARCHAR,
    effect_size_dz   DOUBLE,
    ci_lower         DOUBLE,
    ci_upper         DOUBLE,
    ci_level         DOUBLE,
    mean_baseline    DOUBLE,
    mean_comparison  DOUBLE,
    n_permutations   INTEGER,
    computed_at      TIMESTAMP NOT NULL
);

-- Trial-level condition labels, exported from subjectData.trialinfo upstream.
-- The cycle-feature CSVs contain no condition column, so this is the only place
-- the load condition enters the warehouse.
CREATE TABLE IF NOT EXISTS core.trial_metadata (
    subject_id     VARCHAR NOT NULL,
    trial          INTEGER NOT NULL,
    load_condition INTEGER NOT NULL,
    correct        BOOLEAN,
    run_id         VARCHAR
);
