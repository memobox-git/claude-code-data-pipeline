-- Gold: star schema the agent reads. Built from silver in one transaction for the
-- batch in current_setting('spotify.batch_id'). Gold is a snapshot: emptied and refilled.
-- Every table states its grain in a COMMENT; the agent reads them with list_tables.
--
-- Rule: popularity and audio features belong to the TRACK (dim_track). Reach genres and
-- artists through the bridge tables. Counting bridge rows counts pairs, not tracks.

CREATE SCHEMA IF NOT EXISTS gold;

CREATE TABLE IF NOT EXISTS gold.dim_song (
    song_id int PRIMARY KEY, track_name text NOT NULL, artists text NOT NULL,
    release_count int NOT NULL, max_popularity smallint NOT NULL, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS gold.dim_track (
    track_id text PRIMARY KEY, track_name text NOT NULL, album_name text, artists text NOT NULL,
    primary_artist text NOT NULL, artist_count smallint NOT NULL, song_id int NOT NULL,
    popularity smallint NOT NULL, explicit boolean NOT NULL, duration_sec numeric(7,1) NOT NULL,
    danceability double precision NOT NULL, energy double precision NOT NULL,
    speechiness double precision NOT NULL, acousticness double precision NOT NULL,
    instrumentalness double precision NOT NULL, liveness double precision NOT NULL,
    valence double precision NOT NULL, loudness_db double precision NOT NULL,
    tempo_bpm double precision, musical_key smallint, is_major boolean NOT NULL,
    time_signature smallint, genre_count smallint NOT NULL, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS gold.bridge_track_genre (
    track_id text NOT NULL, genre text NOT NULL, batch_id text NOT NULL, PRIMARY KEY (track_id, genre)
);
CREATE TABLE IF NOT EXISTS gold.dim_genre (
    genre text PRIMARY KEY, track_count int NOT NULL, avg_popularity numeric(5,1) NOT NULL, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS gold.dim_artist (
    artist_id int PRIMARY KEY, artist_name text NOT NULL UNIQUE, track_count int NOT NULL,
    avg_popularity numeric(5,1) NOT NULL, batch_id text NOT NULL
);
CREATE TABLE IF NOT EXISTS gold.bridge_track_artist (
    track_id text NOT NULL, artist_position smallint NOT NULL, artist_id int NOT NULL,
    batch_id text NOT NULL, PRIMARY KEY (track_id, artist_position)
);
CREATE TABLE IF NOT EXISTS gold.catalog_bridge (
    team text NOT NULL, step_no int NOT NULL, step text NOT NULL,
    amount bigint NOT NULL, running_total bigint NOT NULL, batch_id text NOT NULL,
    PRIMARY KEY (team, step_no)
);
CREATE INDEX IF NOT EXISTS bridge_track_genre_genre_idx ON gold.bridge_track_genre (genre);
CREATE INDEX IF NOT EXISTS bridge_track_artist_artist_idx ON gold.bridge_track_artist (artist_id);

TRUNCATE gold.dim_song, gold.dim_track, gold.bridge_track_genre, gold.dim_genre,
         gold.dim_artist, gold.bridge_track_artist, gold.catalog_bridge;

---------------------------------------------------------------- songs: same title + artists across releases
CREATE TEMP TABLE gold_song_key ON COMMIT DROP AS
SELECT track_id, dense_rank() OVER (ORDER BY lower(track_name), lower(artists))::int AS song_id
FROM silver.tracks;

INSERT INTO gold.dim_song (song_id, track_name, artists, release_count, max_popularity, batch_id)
SELECT k.song_id,
       (array_agg(t.track_name ORDER BY t.popularity DESC, t.track_id))[1],
       (array_agg(t.artists    ORDER BY t.popularity DESC, t.track_id))[1],
       count(*), max(t.popularity), current_setting('spotify.batch_id')
FROM silver.tracks t JOIN gold_song_key k USING (track_id)
GROUP BY k.song_id;

---------------------------------------------------------------- tracks
WITH a AS (
    SELECT track_id, count(*) AS n, (array_agg(artist_name ORDER BY artist_position))[1] AS first_artist
    FROM silver.track_artists GROUP BY track_id
), g AS (
    SELECT track_id, count(*) AS n FROM silver.track_genres GROUP BY track_id
)
INSERT INTO gold.dim_track (
    track_id, track_name, album_name, artists, primary_artist, artist_count, song_id,
    popularity, explicit, duration_sec, danceability, energy, speechiness, acousticness,
    instrumentalness, liveness, valence, loudness_db, tempo_bpm, musical_key, is_major,
    time_signature, genre_count, batch_id)
SELECT t.track_id, t.track_name, t.album_name, t.artists, a.first_artist, a.n, k.song_id,
       t.popularity, t.explicit, round(t.duration_ms / 1000.0, 1),
       t.danceability, t.energy, t.speechiness, t.acousticness,
       t.instrumentalness, t.liveness, t.valence, t.loudness_db,
       nullif(t.tempo_bpm, 0), nullif(t.musical_key, -1), t.is_major,
       nullif(t.time_signature, 0), g.n, current_setting('spotify.batch_id')
FROM silver.tracks t
JOIN gold_song_key k USING (track_id)
JOIN a USING (track_id)
JOIN g USING (track_id);

---------------------------------------------------------------- genres
INSERT INTO gold.bridge_track_genre (track_id, genre, batch_id)
SELECT track_id, genre, current_setting('spotify.batch_id') FROM silver.track_genres;

INSERT INTO gold.dim_genre (genre, track_count, avg_popularity, batch_id)
SELECT g.genre, count(*), round(avg(t.popularity), 1), current_setting('spotify.batch_id')
FROM gold.bridge_track_genre g JOIN gold.dim_track t USING (track_id)
GROUP BY g.genre;

---------------------------------------------------------------- artists
INSERT INTO gold.dim_artist (artist_id, artist_name, track_count, avg_popularity, batch_id)
SELECT row_number() OVER (ORDER BY x.artist_name)::int, x.artist_name, count(*),
       round(avg(t.popularity), 1), current_setting('spotify.batch_id')
FROM (SELECT DISTINCT artist_name, track_id FROM silver.track_artists) x
JOIN silver.tracks t USING (track_id)
GROUP BY x.artist_name;

INSERT INTO gold.bridge_track_artist (track_id, artist_position, artist_id, batch_id)
SELECT a.track_id, a.artist_position, d.artist_id, current_setting('spotify.batch_id')
FROM silver.track_artists a JOIN gold.dim_artist d USING (artist_name);

---------------------------------------------------------------- the catalog bridge
-- Each team's count is right for what it counts. These steps walk each one to the agreed
-- number: tracks in gold.dim_track (one row per Spotify track_id, after quarantine).
WITH n AS (
    SELECT
        (SELECT count(*) FROM bronze.tracks WHERE batch_id = current_setting('spotify.batch_id'))                   AS file_rows,
        (SELECT count(DISTINCT track_id) FROM bronze.tracks WHERE batch_id = current_setting('spotify.batch_id'))   AS file_track_ids,
        (SELECT count(*) FROM silver.quarantine WHERE batch_id = current_setting('spotify.batch_id'))               AS quarantined,
        (SELECT coalesce(sum(source_rows), 0) FROM silver.track_genres)                                             AS usable_rows,
        (SELECT count(*) FROM silver.track_genres)                                                                  AS track_genre_pairs,
        (SELECT count(*) FROM gold.dim_track)                                                                       AS tracks,
        (SELECT count(*) FROM gold.dim_song)                                                                        AS songs
), steps (team, step_no, step, amount) AS (
    SELECT 'Label', 1, 'Rows in dataset.csv (Label''s number)', file_rows FROM n
    UNION ALL SELECT 'Label', 2, 'Rows quarantined for bad data', -quarantined FROM n
    UNION ALL SELECT 'Label', 3, 'Duplicate rows: same track twice in the same genre', -(usable_rows - track_genre_pairs) FROM n
    UNION ALL SELECT 'Label', 4, 'Extra rows for tracks listed under more than one genre', -(track_genre_pairs - tracks) FROM n
    UNION ALL SELECT 'Editorial', 1, 'Distinct track_id in dataset.csv (Editorial''s number)', file_track_ids FROM n
    UNION ALL SELECT 'Editorial', 2, 'track_ids whose only rows were quarantined', -(file_track_ids - tracks) FROM n
    UNION ALL SELECT 'Product', 1, 'Distinct songs: same title and artists (Product''s number)', songs FROM n
    UNION ALL SELECT 'Product', 2, 'Extra releases of the same song (album, single, compilation)', tracks - songs FROM n
)
INSERT INTO gold.catalog_bridge (team, step_no, step, amount, running_total, batch_id)
SELECT team, step_no, step, amount,
       sum(amount) OVER (PARTITION BY team ORDER BY step_no), current_setting('spotify.batch_id')
FROM steps;

INSERT INTO gold.catalog_bridge (team, step_no, step, amount, running_total, batch_id)
SELECT DISTINCT ON (team) team, step_no + 1, 'Agreed number: catalog tracks (gold.dim_track)', 0,
       running_total, current_setting('spotify.batch_id')
FROM gold.catalog_bridge
ORDER BY team, step_no DESC;

---------------------------------------------------------------- grain and column comments (the agent reads these)
COMMENT ON TABLE gold.dim_track IS 'Grain: one row = one track (Spotify track_id). The agreed catalog count is count(*) of this table. Popularity and audio features live here: compute them per track, never over bridge rows.';
COMMENT ON TABLE gold.dim_song IS 'Grain: one row = one song: the same title by the same artists (case-insensitive) across all its releases. release_count = number of track_ids.';
COMMENT ON TABLE gold.bridge_track_genre IS 'Grain: one row = one track listed under one genre. A track can be in up to 6 genres, so count(*) here counts track-genre pairs, not tracks. Join to dim_track for per-genre questions.';
COMMENT ON TABLE gold.dim_genre IS 'Grain: one row = one genre (114). The source sampled about 1,000 rows per genre, so track_count reflects the sample design, not market size.';
COMMENT ON TABLE gold.dim_artist IS 'Grain: one row = one artist name. The source has no artist ids, so two artists with the same name are merged.';
COMMENT ON TABLE gold.bridge_track_artist IS 'Grain: one row = one artist credited on one track (artist_position 1 = first credited).';
COMMENT ON TABLE gold.catalog_bridge IS 'Grain: one row = one step from a team''s catalog count to the agreed number (tracks in dim_track). running_total of the last step per team = agreed number.';

COMMENT ON COLUMN gold.dim_track.popularity IS 'Spotify popularity 0-100 at snapshot time; highest value across the track''s source rows.';
COMMENT ON COLUMN gold.dim_track.explicit IS 'true = explicit lyrics.';
COMMENT ON COLUMN gold.dim_track.duration_sec IS 'Track length in seconds.';
COMMENT ON COLUMN gold.dim_track.danceability IS '0-1, Spotify audio feature.';
COMMENT ON COLUMN gold.dim_track.energy IS '0-1, Spotify audio feature.';
COMMENT ON COLUMN gold.dim_track.speechiness IS '0-1; above about 0.66 is mostly spoken word.';
COMMENT ON COLUMN gold.dim_track.acousticness IS '0-1 confidence the track is acoustic.';
COMMENT ON COLUMN gold.dim_track.instrumentalness IS '0-1; above 0.5 likely has no vocals.';
COMMENT ON COLUMN gold.dim_track.liveness IS '0-1; above 0.8 likely a live recording.';
COMMENT ON COLUMN gold.dim_track.valence IS '0-1 musical positiveness (high = happy).';
COMMENT ON COLUMN gold.dim_track.loudness_db IS 'Average loudness in dB, usually -60 to 0.';
COMMENT ON COLUMN gold.dim_track.tempo_bpm IS 'Beats per minute. NULL when Spotify reported 0 (not detected).';
COMMENT ON COLUMN gold.dim_track.musical_key IS 'Pitch class: 0 = C, 1 = C#/Db ... 11 = B. NULL when not detected.';
COMMENT ON COLUMN gold.dim_track.is_major IS 'true = major mode, false = minor.';
COMMENT ON COLUMN gold.dim_track.time_signature IS 'Beats per bar. NULL when Spotify reported 0 (not detected).';
COMMENT ON COLUMN gold.dim_track.genre_count IS 'Number of genres this track is listed under.';
COMMENT ON COLUMN gold.dim_track.primary_artist IS 'First credited artist.';
COMMENT ON COLUMN gold.dim_track.song_id IS 'Groups re-releases of the same song; joins gold.dim_song.';
COMMENT ON COLUMN gold.dim_genre.avg_popularity IS 'Average popularity of the tracks listed under this genre.';
COMMENT ON COLUMN gold.dim_artist.track_count IS 'Distinct tracks credited to this artist.';
COMMENT ON COLUMN gold.dim_artist.avg_popularity IS 'Average popularity of this artist''s tracks.';
