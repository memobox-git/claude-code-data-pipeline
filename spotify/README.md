# Spotify data agent

A data agent answers business questions by **querying data**, not by guessing. This one is
Claude running in a loop with four read-only tools over a Postgres star schema built from
Kaggle's [Spotify tracks dataset](https://www.kaggle.com/datasets/maharshipandya/-spotify-tracks-dataset).
It follows the same contract as the Olist pipeline in this repo: raw, bronze, silver, gold
and meta layers, quality checks, lineage, and one agreed number with a bridge.

## The situation

Three teams report three catalog sizes. Each is right for what it counts.

| Team | Their number | What they count |
|---|---|---|
| Label | 114,000 | Rows in `dataset.csv` |
| Editorial | 89,741 | Distinct `track_id` values in the file |
| Product | 81,206 | Distinct songs (same title and artists) |
| **Agreed** | **89,740** | **Tracks in `gold.dim_track`** |

`gold.catalog_bridge` walks each team's number to the agreed one:

| Team | Step | Rows | Running total |
|---|---|---|---|
| Label | Rows in dataset.csv | 114,000 | 114,000 |
| | Rows quarantined for bad data | −1 | 113,999 |
| | Duplicate rows: same track twice in the same genre | −450 | 113,549 |
| | Extra rows for tracks listed under more than one genre | −23,809 | **89,740** |
| Editorial | Distinct track_id in dataset.csv | 89,741 | 89,741 |
| | track_ids whose only rows were quarantined | −1 | **89,740** |
| Product | Distinct songs | 81,206 | 81,206 |
| | Extra releases of the same song | +8,534 | **89,740** |

**Business question:** what makes a track popular: genre, sound, explicit content?

## What the agent does

| You ask | It does |
|---|---|
| How many tracks do we have? | Reads the agreed definition, runs it, and explains the bridge |
| Do explicit tracks get higher popularity? | Compares per-track averages (36.9 vs 32.9), with counts, and says it's a correlation |
| Which genre is most popular? | Goes through the genre bridge, and warns that genres are a 1,000-per-genre sample |
| Delete the bad rows | Refuses: it can only read |
| How many streams did X get? | Says the data has no streams instead of making something up |

Every answer ends with the SQL it ran.

```
Kaggle CSV -> raw (S3 or data/) -> bronze -> silver -> gold (+ meta)
                                                    |
                                    metrics.yaml (agreed definitions)
                                                    |
question -> Claude + tools -> answer, table, caveats, SQL
            list_tables | describe_table | get_metric | run_sql
            as agent_reader: SELECT on gold only, read-only, 15 s timeout, 200 rows
            log -> data/agent_log.jsonl
```

## Files

| Path | What |
|---|---|
| `pipeline.py` | CLI: `land`, `bronze`, `silver`, `gold`, `role`, `all` |
| `land_raw.py`, `load_bronze.py` | Raw zone + manifest; bronze with header and row-count block checks |
| `build_layer.py` | Silver or gold in one transaction: SQL, checks, lineage; block check fails = rollback |
| `sql/20_silver.sql` | Types, quarantine with reasons, one row per track, genre and artist tables |
| `sql/30_gold.sql` | Star schema with grain comments, and `catalog_bridge` |
| `sql/*_checks.sql` | Data-quality checks: each returns the failing rows, `block` or `warn` |
| `sql/40_agent_role.sql`, `agent_role.py` | The `agent_reader` login |
| `metrics.yaml` | The semantic layer: agreed metric definitions and caveats |
| `tools.py` | The four tools and their guardrails |
| `agent.py` | The agent loop and CLI |
| `evals.yaml`, `evals.py` | Golden questions and the grader |
| `tests/` | Guardrails, pipeline numbers, the agent loop (with a fake Claude), the grader |

## Data rules

- **Popularity and audio features belong to the track.** Compute them on `gold.dim_track`.
  Reach genres and artists through the bridge tables. Counting bridge rows counts pairs,
  not tracks. This is the Spotify version of Olist's "never join items to payments directly".
- Silver keeps one row per `track_id`. A track's rows agree on everything except
  popularity (a block check proves it); silver keeps the highest popularity and records
  the lowest in `popularity_min`.
- Gold turns `tempo = 0` and `time_signature = 0` into NULL (not detected). Silver keeps the
  original values, and warning checks count them.
- Genre sizes come from the source's sampling (about 1,000 listings per genre), not the market.

## Run it locally

Any Postgres 14+ works. Point both URLs at it: the pipeline writes as an admin, and the
agent reads as `agent_reader`, which the `role` step creates.

```bash
export SPOTIFY_DATABASE_URL="postgresql://postgres@localhost:5432/spotify"
```

```bash
export SPOTIFY_AGENT_DATABASE_URL="postgresql://agent_reader@localhost:5432/spotify"
```

```bash
kaggle datasets download -d maharshipandya/-spotify-tracks-dataset -p data/spotify --unzip
```

```bash
python -m spotify.pipeline all data/spotify
```

```bash
python -m pytest spotify/tests -q
```

Locally `agent_reader` has no password, so the server must trust it on the local socket.
Otherwise give it one and put it in the URL. Never commit that.

Ask questions (needs a Claude API key in `ANTHROPIC_API_KEY`):

```bash
python -m spotify "Do explicit tracks get higher popularity than clean ones?"
```

```bash
python -m spotify --show-sql
```

Run the evals (about 10 questions, roughly 3-6 model calls each):

```bash
python -m spotify.evals
```

Settings, all optional: `AGENT_MODEL` (default `claude-opus-5-5`), `AGENT_EFFORT` (`high`),
`AGENT_MAX_STEPS` (15), `AGENT_STATEMENT_TIMEOUT` (`15s`), `AGENT_LOG`.

## Run it on AWS

Nothing here has been created in AWS yet. Each step below needs approval first (CLAUDE.md).

| Step | Resource |
|---|---|
| 1 | Database `spotify` on the existing `lightning-demo-rds` instance |
| 2 | Raw files under `raw/spotify/` in the existing bucket (`SPOTIFY_RAW=s3`) |
| 3 | A Secrets Manager secret holding `agent_reader`'s password (`AGENT_SECRET_NAME`), tagged `project=olist-lightning` |
| 4 | Run the pipeline from the allowed IP, or add Spotify steps to the Lambda the way `src/handler.py` runs Olist's |

With `SPOTIFY_DATABASE_URL` and `SPOTIFY_AGENT_DATABASE_URL` empty, everything connects to
RDS with passwords fetched from Secrets Manager at runtime.
