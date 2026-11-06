"""Build silver or gold for one batch in ONE transaction:
  1. run sql/<layer>.sql (reads current_setting('spotify.batch_id'))
  2. run every check in sql/<layer>_checks.sql and record it in meta.dq_results
  3. record source -> target row counts in meta.lineage
  4. any failed 'block' check rolls the whole layer back; 'warn' is recorded only
"""
from __future__ import annotations

import json
import re

from .common import SQL_DIR, admin_connect

LAYERS = {
    "silver": {
        "sql": "20_silver.sql", "checks": "25_silver_checks.sql",
        # (source, target, count quarantined rows from this source?)
        "lineage": [("bronze.tracks", "silver.tracks", True),
                    ("bronze.tracks", "silver.track_genres", True),
                    ("silver.tracks", "silver.track_artists", False)],
    },
    "gold": {
        "sql": "30_gold.sql", "checks": "35_gold_checks.sql",
        "lineage": [("silver.tracks", "gold.dim_track", False),
                    ("silver.tracks", "gold.dim_song", False),
                    ("silver.track_genres", "gold.bridge_track_genre", False),
                    ("silver.track_genres", "gold.dim_genre", False),
                    ("silver.track_artists", "gold.bridge_track_artist", False),
                    ("silver.track_artists", "gold.dim_artist", False)],
    },
}
CHECK_HDR = re.compile(r"^--\s*check:\s*(?P<name>[\w-]+)\s*\|\s*(?P<sev>block|warn)\s*\|\s*(?P<target>[\w.]+)\s*$", re.M)


def parse_checks(text: str) -> list[dict]:
    parts = CHECK_HDR.split(text)  # [prelude, name, sev, target, body, name, sev, target, body, ...]
    return [{"name": parts[i], "severity": parts[i + 1], "target": parts[i + 2],
             "sql": parts[i + 3].strip().rstrip(";")} for i in range(1, len(parts), 4)]


def _count(cur, table: str, batch_id: str) -> int:
    schema, name = table.split(".")
    cur.execute(f'SELECT count(*) FROM "{schema}"."{name}" WHERE batch_id = %s', (batch_id,))
    return cur.fetchone()[0]


def run(layer: str, batch_id: str) -> dict:
    spec = LAYERS[layer]
    checks_out, blocks = [], []
    with admin_connect() as conn:
        cur = conn.cursor()
        cur.execute("SELECT set_config('spotify.batch_id', %s, true)", (batch_id,))
        cur.execute((SQL_DIR / spec["sql"]).read_text())

        for chk in parse_checks((SQL_DIR / spec["checks"]).read_text()):
            cur.execute(f"SELECT count(*) FROM ({chk['sql']}) f")
            failing = cur.fetchone()[0]
            sample = None
            if failing:
                cur.execute(f"SELECT to_jsonb(f) FROM ({chk['sql']}) f LIMIT 5")
                sample = json.dumps([r[0] for r in cur.fetchall()], default=str)
            cur.execute(
                "INSERT INTO meta.dq_results (batch_id, layer, check_name, target, severity, failing_rows, sample) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (batch_id, layer, chk["name"], chk["target"], chk["severity"], failing, sample))
            status = "ok" if failing == 0 else chk["severity"].upper()
            checks_out.append({"check": chk["name"], "severity": chk["severity"], "failing_rows": failing, "status": status})
            print(f"check {chk['name']:38} {chk['severity']:5} failing={failing:<7} {status}", flush=True)
            if failing and chk["severity"] == "block":
                blocks.append(f"{chk['name']}: {failing} failing rows, sample {sample}")

        lineage = {}
        for source, target, quarantine in spec["lineage"]:
            rows_in, rows_out = _count(cur, source, batch_id), _count(cur, target, batch_id)
            quarantined = 0
            if quarantine:
                cur.execute("SELECT count(*) FROM silver.quarantine WHERE batch_id = %s AND source_table = %s",
                            (batch_id, source))
                quarantined = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO meta.lineage (batch_id, layer, source, target, rows_in, rows_out, rows_quarantined) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (batch_id, layer, source, target, rows_in, rows_out, quarantined))
            lineage[target] = {"source": source, "rows_in": rows_in, "rows_out": rows_out, "quarantined": quarantined}
            print(f"{source:22} -> {target:26} in={rows_in:<8} out={rows_out:<8} quarantined={quarantined}", flush=True)

        if blocks:
            conn.rollback()
            raise RuntimeError(f"{layer} rolled back, block checks failed:\n  " + "\n  ".join(blocks))
        conn.commit()
        conn.autocommit = True
        cur.execute("ANALYZE")  # fresh planner statistics for the agent's queries
    print(f"{layer} batch {batch_id} committed\n", flush=True)
    return {"checks": checks_out, "lineage": lineage}
