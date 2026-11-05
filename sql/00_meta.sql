-- Metadata schema: batches, lineage, data-quality results. Idempotent.

CREATE SCHEMA IF NOT EXISTS meta;

CREATE TABLE IF NOT EXISTS meta.batch (
    batch_id    text PRIMARY KEY,
    source      text NOT NULL,
    landed_at   timestamptz,
    registered_at timestamptz NOT NULL DEFAULT now()
);

-- One row = one step moving rows from one source to one target in one batch.
CREATE TABLE IF NOT EXISTS meta.lineage (
    lineage_id   bigserial PRIMARY KEY,
    batch_id     text NOT NULL REFERENCES meta.batch(batch_id),
    layer        text NOT NULL,            -- bronze | silver | gold
    source       text NOT NULL,            -- s3 key, or schema.table
    target       text NOT NULL,            -- schema.table
    rows_in      bigint,
    rows_out     bigint,
    rows_quarantined bigint NOT NULL DEFAULT 0,
    run_at       timestamptz NOT NULL DEFAULT now()
);

-- One row = one data-quality check run on one table in one batch.
CREATE TABLE IF NOT EXISTS meta.dq_results (
    dq_id        bigserial PRIMARY KEY,
    batch_id     text NOT NULL REFERENCES meta.batch(batch_id),
    layer        text NOT NULL,
    check_name   text NOT NULL,
    target       text NOT NULL,
    severity     text NOT NULL CHECK (severity IN ('block', 'warn')),
    failing_rows bigint NOT NULL,
    passed       boolean GENERATED ALWAYS AS (failing_rows = 0) STORED,
    sample       jsonb,                    -- up to 5 failing rows
    run_at       timestamptz NOT NULL DEFAULT now()
);
