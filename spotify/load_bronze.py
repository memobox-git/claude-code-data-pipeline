"""Step 2: load one raw batch into bronze, in one transaction.

  - block check: the CSV header matches the contract (schema drift stops the load)
  - COPY the file into bronze.tracks, all text, plus batch_id / source_file
  - block check: rows loaded == rows in the manifest
  - lineage: raw file -> bronze.tracks with row counts
Re-running a batch first deletes that batch's bronze rows (idempotent).
"""
from __future__ import annotations

import json

from psycopg import sql

from .common import SQL_DIR, RawStore, admin_connect

FILE_TO_TABLE = {"dataset.csv": "tracks"}
CONTRACT = {
    "tracks": ["", "track_id", "artists", "album_name", "track_name", "popularity", "duration_ms",
               "explicit", "danceability", "energy", "key", "loudness", "mode", "speechiness",
               "acousticness", "instrumentalness", "liveness", "valence", "tempo", "time_signature",
               "track_genre"],
}
RENAME = {"": "row_index"}  # the unnamed pandas index column


def _dq(cur, batch_id: str, check: str, target: str, failing: int, sample: dict) -> None:
    cur.execute(
        "INSERT INTO meta.dq_results (batch_id, layer, check_name, target, severity, failing_rows, sample) "
        "VALUES (%s, 'bronze', %s, %s, 'block', %s, %s)",
        (batch_id, check, target, failing, json.dumps(sample)),
    )


def run(batch_id: str) -> dict:
    store = RawStore(batch_id)
    manifest = store.get_json("_manifest.json")
    failures: list[str] = []
    result: dict[str, dict] = {}

    with admin_connect() as conn:  # one transaction; commit only at the end
        cur = conn.cursor()
        cur.execute((SQL_DIR / "00_meta.sql").read_text())
        cur.execute((SQL_DIR / "10_bronze.sql").read_text())
        cur.execute("INSERT INTO meta.batch (batch_id, source, landed_at) VALUES (%s, %s, %s) "
                    "ON CONFLICT (batch_id) DO NOTHING",
                    (batch_id, manifest["source"], manifest["landed_at"]))

        for f in manifest["files"]:
            table = FILE_TO_TABLE[f["file"]]
            target = f"bronze.{table}"
            header_ok = f["columns"] == CONTRACT[table]
            _dq(cur, batch_id, "header_matches_contract", target, 0 if header_ok else 1,
                {"expected": CONTRACT[table], "got": f["columns"]})
            if not header_ok:
                failures.append(f"{target}: header does not match the contract")
                continue

            columns = [RENAME.get(c, c) for c in f["columns"]]
            cols = sql.SQL(", ").join(sql.Identifier(c) for c in columns)
            ident = sql.Identifier("bronze", table)
            cur.execute(sql.SQL("DELETE FROM {} WHERE batch_id = %s").format(ident), (batch_id,))

            stage = sql.Identifier(f"stage_{table}")
            cur.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                stage, sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in columns)))
            copy_stmt = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')").format(stage)
            with cur.copy(copy_stmt) as copy:
                for chunk in store.chunks(f["key"]):
                    copy.write(chunk)
            cur.execute(sql.SQL("INSERT INTO {} ({}, batch_id, source_file) SELECT {}, %s, %s FROM {}")
                        .format(ident, cols, cols, stage), (batch_id, f["key"]))
            loaded = cur.rowcount

            cur.execute("INSERT INTO meta.lineage (batch_id, layer, source, target, rows_in, rows_out) "
                        "VALUES (%s, 'bronze', %s, %s, %s, %s)", (batch_id, f["key"], target, f["rows"], loaded))
            failing = abs(loaded - f["rows"])
            _dq(cur, batch_id, "row_count_matches_manifest", target, failing,
                {"manifest_rows": f["rows"], "loaded_rows": loaded})
            if failing:
                failures.append(f"{target}: manifest {f['rows']} vs loaded {loaded}")
            result[target] = {"manifest": f["rows"], "loaded": loaded, "check": "FAIL" if failing else "ok"}
            print(f"{target:20} manifest={f['rows']:<8} loaded={loaded:<8} {'FAIL' if failing else 'ok'}", flush=True)

        if failures:
            conn.rollback()
            raise RuntimeError("block check failed, bronze rolled back:\n  " + "\n  ".join(failures))
        conn.commit()
    print(f"bronze batch {batch_id} committed", flush=True)
    return result
