-- Silver: correct types, one row per real thing, bad rows quarantined with a reason.
-- Runs in one transaction for the batch in current_setting('spotify.batch_id').
-- Silver holds the batch being built: its tables are emptied and refilled.
-- The quarantine keeps every batch's refused rows.

CREATE SCHEMA IF NOT EXISTS silver;

-- Grain: one row = one bronze row that silver refused, and why.
CREATE TABLE IF NOT EXISTS silver.quarantine (
    quarantine_id  bigserial PRIMARY KEY,
    batch_id       text NOT NULL,
    source_table   text NOT NULL,
    reason         text NOT NULL,
    row_data       jsonb NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);

-- Grain: one row = one track (Spotify track_id).
CREATE TABLE IF NOT EXISTS silver.tracks (
    track_id text PRIMARY KEY,
    track_name text NOT NULL, album_name text, artists text NOT NULL,
    popularity     smallint NOT NULL,   -- highest value across the track's source rows
    popularity_min smallint NOT NULL,   -- lowest value; differs from popularity for a few hundred tracks
    duration_ms int NOT NULL, explicit boolean NOT NULL,
    danceability double precision NOT NULL, energy double precision NOT NULL,
    speechiness double precision NOT NULL, acousticness double precision NOT NULL,
    instrumentalness double precision NOT NULL, liveness double precision NOT NULL,
    valence double precision NOT NULL,
    loudness_db double precision NOT NULL, tempo_bpm double precision NOT NULL,
    musical_key smallint NOT NULL, is_major boolean NOT NULL, time_signature smallint NOT NULL,
    source_rows int NOT NULL,           -- bronze rows collapsed into this track
    batch_id text NOT NULL
);
-- Grain: one row = one track listed under one genre.
CREATE TABLE IF NOT EXISTS silver.track_genres (
    track_id text NOT NULL, genre text NOT NULL,
    source_rows int NOT NULL,           -- >1 when the file repeats the same track in the same genre
    batch_id text NOT NULL,
    PRIMARY KEY (track_id, genre)
);
-- Grain: one row = one artist credited on one track, in credit order.
CREATE TABLE IF NOT EXISTS silver.track_artists (
    track_id text NOT NULL, artist_position smallint NOT NULL, artist_name text NOT NULL,
    batch_id text NOT NULL,
    PRIMARY KEY (track_id, artist_position)
);

TRUNCATE silver.track_artists, silver.track_genres, silver.tracks;
DELETE FROM silver.quarantine WHERE batch_id = current_setting('spotify.batch_id');

-- Safe casts return NULL instead of raising. Numbers may be in scientific notation (1.01e-06).
CREATE OR REPLACE FUNCTION silver.to_num(t text) RETURNS double precision LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN btrim(t) ~ '^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$' THEN btrim(t)::double precision END $$;
CREATE OR REPLACE FUNCTION silver.to_int(t text) RETURNS int LANGUAGE sql IMMUTABLE AS
$$ SELECT CASE WHEN btrim(t) ~ '^[-+]?\d{1,9}$' THEN btrim(t)::int END $$;
CREATE OR REPLACE FUNCTION silver.in_0_1(x double precision) RETURNS boolean LANGUAGE sql IMMUTABLE AS
$$ SELECT x IS NOT NULL AND x BETWEEN 0 AND 1 $$;

---------------------------------------------------------------- type every row once
CREATE TEMP TABLE silver_typed ON COMMIT DROP AS
WITH t AS (
    SELECT to_jsonb(b) - 'batch_id' - 'source_file' - 'loaded_at' AS raw,
           silver.to_int(b.row_index)            AS row_no,
           btrim(b.track_id)                     AS track_id,
           btrim(b.track_name)                   AS track_name,
           nullif(btrim(b.album_name), '')       AS album_name,
           btrim(b.artists)                      AS artists,
           btrim(b.track_genre)                  AS genre,
           silver.to_int(b.popularity)           AS popularity,
           silver.to_int(b.duration_ms)          AS duration_ms,
           CASE b.explicit WHEN 'True' THEN true WHEN 'False' THEN false END AS explicit,
           silver.to_num(b.danceability)         AS danceability,
           silver.to_num(b.energy)               AS energy,
           silver.to_num(b.speechiness)          AS speechiness,
           silver.to_num(b.acousticness)         AS acousticness,
           silver.to_num(b.instrumentalness)     AS instrumentalness,
           silver.to_num(b.liveness)             AS liveness,
           silver.to_num(b.valence)              AS valence,
           silver.to_num(b.loudness)             AS loudness_db,
           silver.to_num(b.tempo)                AS tempo_bpm,
           silver.to_int(b.key)                  AS musical_key,
           silver.to_int(b.mode)                 AS mode,
           silver.to_int(b.time_signature)       AS time_signature
    FROM bronze.tracks b
    WHERE b.batch_id = current_setting('spotify.batch_id')
)
SELECT t.*,
    CASE
        WHEN t.track_id IS NULL OR t.track_id !~ '^[0-9A-Za-z]{22}$'
            THEN 'track_id is not a 22-character Spotify id'
        WHEN coalesce(t.track_name, '') = '' OR coalesce(t.artists, '') = ''
            THEN 'missing track name or artists'
        WHEN coalesce(t.genre, '') = ''
            THEN 'missing genre'
        WHEN t.popularity IS NULL OR t.popularity NOT BETWEEN 0 AND 100
            THEN 'popularity is not an integer 0-100'
        WHEN t.duration_ms IS NULL OR t.duration_ms < 0
            THEN 'duration_ms is not a non-negative integer'
        WHEN t.explicit IS NULL
            THEN 'explicit is not True or False'
        WHEN NOT (silver.in_0_1(t.danceability) AND silver.in_0_1(t.energy) AND silver.in_0_1(t.speechiness)
                  AND silver.in_0_1(t.acousticness) AND silver.in_0_1(t.instrumentalness)
                  AND silver.in_0_1(t.liveness) AND silver.in_0_1(t.valence))
            THEN 'an audio feature is missing or outside 0-1'
        WHEN t.loudness_db IS NULL OR t.tempo_bpm IS NULL OR t.tempo_bpm < 0
            THEN 'loudness or tempo is not a valid number'
        WHEN t.musical_key IS NULL OR t.musical_key NOT BETWEEN -1 AND 11
             OR t.mode IS NULL OR t.mode NOT IN (0, 1)
             OR t.time_signature IS NULL OR t.time_signature NOT BETWEEN 0 AND 7
            THEN 'key, mode or time_signature out of range'
    END AS reject_reason
FROM t;

---------------------------------------------------------------- quarantine
INSERT INTO silver.quarantine (batch_id, source_table, reason, row_data)
SELECT current_setting('spotify.batch_id'), 'bronze.tracks', reject_reason, raw
FROM silver_typed WHERE reject_reason IS NOT NULL;

---------------------------------------------------------------- tracks: one row per track_id
-- The file lists a track once per genre. Apart from popularity the rows agree
-- (checked in 25_silver_checks.sql); keep the first row and the highest popularity.
INSERT INTO silver.tracks (
    track_id, track_name, album_name, artists, popularity, popularity_min, duration_ms, explicit,
    danceability, energy, speechiness, acousticness, instrumentalness, liveness, valence,
    loudness_db, tempo_bpm, musical_key, is_major, time_signature, source_rows, batch_id)
SELECT DISTINCT ON (track_id)
       track_id, track_name, album_name, artists,
       max(popularity) OVER w, min(popularity) OVER w, duration_ms, explicit,
       danceability, energy, speechiness, acousticness, instrumentalness, liveness, valence,
       loudness_db, tempo_bpm, musical_key, mode = 1, time_signature,
       count(*) OVER w, current_setting('spotify.batch_id')
FROM silver_typed
WHERE reject_reason IS NULL
WINDOW w AS (PARTITION BY track_id)
ORDER BY track_id, row_no;

---------------------------------------------------------------- track x genre
INSERT INTO silver.track_genres (track_id, genre, source_rows, batch_id)
SELECT track_id, genre, count(*), current_setting('spotify.batch_id')
FROM silver_typed WHERE reject_reason IS NULL
GROUP BY track_id, genre;

---------------------------------------------------------------- track x artist ("A;B" -> two rows)
INSERT INTO silver.track_artists (track_id, artist_position, artist_name, batch_id)
SELECT t.track_id, a.pos, btrim(a.name), t.batch_id
FROM silver.tracks t
CROSS JOIN LATERAL unnest(string_to_array(t.artists, ';')) WITH ORDINALITY AS a(name, pos)
WHERE btrim(a.name) <> '';
