-- Gold data-quality checks. Each query returns the FAILING rows (zero rows = pass).
-- Header format:  -- check: <name> | <severity block|warn> | <target table>

-- check: one_gold_track_per_silver_track | block | gold.dim_track
SELECT (SELECT count(*) FROM gold.dim_track) AS gold_tracks, (SELECT count(*) FROM silver.tracks) AS silver_tracks
WHERE (SELECT count(*) FROM gold.dim_track) <> (SELECT count(*) FROM silver.tracks);

-- check: bridge_ends_at_agreed_number | block | gold.catalog_bridge
SELECT team, running_total FROM gold.catalog_bridge b
WHERE step_no = (SELECT max(step_no) FROM gold.catalog_bridge WHERE team = b.team)
  AND running_total <> (SELECT count(*) FROM gold.dim_track);

-- check: song_releases_add_up | block | gold.dim_song
SELECT sum(release_count) AS releases FROM gold.dim_song
HAVING sum(release_count) <> (SELECT count(*) FROM gold.dim_track);

-- check: every_track_in_a_genre | block | gold.dim_track
SELECT t.track_id FROM gold.dim_track t
LEFT JOIN gold.bridge_track_genre g USING (track_id) WHERE g.track_id IS NULL;

-- check: genre_bridge_tracks_exist | block | gold.bridge_track_genre
SELECT g.track_id, g.genre FROM gold.bridge_track_genre g
LEFT JOIN gold.dim_track t USING (track_id) WHERE t.track_id IS NULL;

-- check: artist_bridge_tracks_exist | block | gold.bridge_track_artist
SELECT a.track_id, a.artist_id FROM gold.bridge_track_artist a
LEFT JOIN gold.dim_track t USING (track_id) WHERE t.track_id IS NULL;

-- check: popularity_0_to_100 | block | gold.dim_track
SELECT track_id, popularity FROM gold.dim_track WHERE popularity NOT BETWEEN 0 AND 100;

-- check: audio_features_0_to_1 | block | gold.dim_track
SELECT track_id FROM gold.dim_track
WHERE NOT (danceability BETWEEN 0 AND 1 AND energy BETWEEN 0 AND 1 AND speechiness BETWEEN 0 AND 1
       AND acousticness BETWEEN 0 AND 1 AND instrumentalness BETWEEN 0 AND 1
       AND liveness BETWEEN 0 AND 1 AND valence BETWEEN 0 AND 1);

-- check: genre_counts_match_bridge | block | gold.dim_genre
SELECT d.genre, d.track_count, count(g.track_id) AS bridge_tracks
FROM gold.dim_genre d LEFT JOIN gold.bridge_track_genre g USING (genre)
GROUP BY d.genre, d.track_count HAVING d.track_count <> count(g.track_id);
