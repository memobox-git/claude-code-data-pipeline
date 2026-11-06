-- Silver data-quality checks. Each query returns the FAILING rows (zero rows = pass).
-- Header format:  -- check: <name> | <severity block|warn> | <target table>

-- check: bronze_rows_reach_silver | block | silver.track_genres
-- Every bronze row is either quarantined or present as a (track, genre) pair.
SELECT b.row_index, b.track_id, b.track_genre
FROM bronze.tracks b
LEFT JOIN silver.quarantine q
       ON q.batch_id = b.batch_id AND q.row_data->>'row_index' = b.row_index
LEFT JOIN silver.track_genres g
       ON g.track_id = btrim(b.track_id) AND g.genre = btrim(b.track_genre)
WHERE b.batch_id = current_setting('spotify.batch_id') AND q.quarantine_id IS NULL AND g.track_id IS NULL;

-- check: every_track_has_genre | block | silver.tracks
SELECT t.track_id FROM silver.tracks t
LEFT JOIN silver.track_genres g USING (track_id) WHERE g.track_id IS NULL;

-- check: every_genre_row_has_track | block | silver.track_genres
SELECT g.track_id, g.genre FROM silver.track_genres g
LEFT JOIN silver.tracks t USING (track_id) WHERE t.track_id IS NULL;

-- check: every_track_has_artist | block | silver.tracks
SELECT t.track_id, t.artists FROM silver.tracks t
LEFT JOIN silver.track_artists a USING (track_id) WHERE a.track_id IS NULL;

-- check: quarantine_rate_under_1pct | block | silver.quarantine
SELECT count(*) AS quarantined FROM silver.quarantine
WHERE batch_id = current_setting('spotify.batch_id')
HAVING count(*) > 0.01 * (SELECT count(*) FROM bronze.tracks WHERE batch_id = current_setting('spotify.batch_id'));

-- check: track_attributes_agree_across_rows | block | silver.tracks
-- Silver keeps one row per track, so its rows must agree on everything except popularity.
SELECT btrim(b.track_id) AS track_id
FROM bronze.tracks b
JOIN silver.tracks t ON t.track_id = btrim(b.track_id)
WHERE b.batch_id = current_setting('spotify.batch_id')
GROUP BY btrim(b.track_id)
HAVING count(DISTINCT ROW(b.track_name, b.album_name, b.artists, b.duration_ms, b.explicit,
                          b.danceability, b.energy, b.key, b.loudness, b.mode, b.speechiness,
                          b.acousticness, b.instrumentalness, b.liveness, b.valence, b.tempo,
                          b.time_signature)) > 1;

-- check: popularity_differs_across_rows | warn | silver.tracks
-- Silver keeps the highest value; popularity_min holds the lowest.
SELECT track_id, popularity_min, popularity FROM silver.tracks WHERE popularity <> popularity_min;

-- check: same_track_twice_in_a_genre | warn | silver.track_genres
SELECT track_id, genre, source_rows FROM silver.track_genres WHERE source_rows > 1;

-- check: tempo_not_detected | warn | silver.tracks
SELECT track_id, tempo_bpm FROM silver.tracks WHERE tempo_bpm = 0;

-- check: time_signature_not_detected | warn | silver.tracks
SELECT track_id, time_signature FROM silver.tracks WHERE time_signature = 0;

-- check: duration_under_30s | warn | silver.tracks
SELECT track_id, track_name, duration_ms FROM silver.tracks WHERE duration_ms < 30000;
