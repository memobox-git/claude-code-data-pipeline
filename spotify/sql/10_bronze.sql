-- Bronze: dataset.csv exactly as landed. Every source column is text.
-- Adds batch_id, source_file, loaded_at. Idempotent.
-- The CSV's first column has no header (a pandas index); bronze names it row_index.

CREATE SCHEMA IF NOT EXISTS bronze;

-- Grain: one row = one line of dataset.csv, i.e. one track listed under one genre.
CREATE TABLE IF NOT EXISTS bronze.tracks (
    row_index text, track_id text, artists text, album_name text, track_name text,
    popularity text, duration_ms text, explicit text, danceability text, energy text,
    key text, loudness text, mode text, speechiness text, acousticness text,
    instrumentalness text, liveness text, valence text, tempo text, time_signature text,
    track_genre text,
    batch_id text NOT NULL, source_file text NOT NULL, loaded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tracks_batch_idx ON bronze.tracks (batch_id);
COMMENT ON TABLE bronze.tracks IS 'Grain: one row = one line of dataset.csv (one track listed under one genre). All text, as landed.';
