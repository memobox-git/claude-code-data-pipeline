"""Step 2: load one S3 raw batch into bronze, in one transaction.

Usage:
    python src/load_bronze.py --batch-id 20261105T224756Z

For each file in the batch manifest:
  - stream the CSV from S3 straight into bronze.<table> with COPY (all text)
  - record source -> target with row counts in meta.lineage
  - block check: rows loaded == rows in manifest
If any block check fails, the whole transaction rolls back and nothing is kept.
Re-running the same batch id first deletes that batch's rows (idempotent).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from psycopg import sql

from common import ROOT, aws_session, db_connect, settings

FILE_TO_TABLE = {
    "olist_orders_dataset.csv": "orders",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_customers_dataset.csv": "customers",
    "olist_sellers_dataset.csv": "sellers",
    "olist_products_dataset.csv": "products",
    "product_category_name_translation.csv": "product_category_translation",
    "olist_geolocation_dataset.csv": "geolocation",
}


def run_sql_file(cur, path: Path) -> None:
    cur.execute(path.read_text())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-id", required=True)
    args = ap.parse_args()

    cfg = settings()
    session = aws_session(cfg)
    s3 = session.client("s3")
    bucket, prefix = cfg["S3_BUCKET"], f"raw/olist/batch={args.batch_id}/"
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=prefix + "_manifest.json")["Body"].read())

    failures: list[str] = []
    with db_connect(cfg, session) as conn:          # one transaction; commit only at the end
        cur = conn.cursor()
        run_sql_file(cur, ROOT / "sql" / "00_meta.sql")
        run_sql_file(cur, ROOT / "sql" / "10_bronze.sql")
        cur.execute(
            "INSERT INTO meta.batch (batch_id, source, landed_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (batch_id) DO NOTHING",
            (manifest["batch_id"], manifest["source"], manifest["landed_at"]),
        )

        print(f"{'table':32} {'manifest':>9} {'loaded':>9}  check")
        for f in manifest["files"]:
            table = FILE_TO_TABLE[f["file"]]
            target = sql.Identifier("bronze", table)
            cols = sql.SQL(", ").join(sql.Identifier(c) for c in f["columns"])

            cur.execute(sql.SQL("DELETE FROM {} WHERE batch_id = %s").format(target), (args.batch_id,))

            # COPY the CSV body into a temp table, then insert with the audit columns.
            stage = sql.Identifier(f"stage_{table}")
            cur.execute(sql.SQL("CREATE TEMP TABLE {} ({}) ON COMMIT DROP").format(
                stage, sql.SQL(", ").join(sql.SQL("{} text").format(sql.Identifier(c)) for c in f["columns"])))
            body = s3.get_object(Bucket=bucket, Key=f["s3_key"])["Body"]
            copy_stmt = sql.SQL("COPY {} FROM STDIN WITH (FORMAT csv, HEADER true, NULL '')").format(stage)
            with cur.copy(copy_stmt) as copy:
                for chunk in body.iter_chunks(1 << 20):
                    copy.write(chunk)
            cur.execute(
                sql.SQL("INSERT INTO {} ({}, batch_id, source_file) SELECT {}, %s, %s FROM {}")
                .format(target, cols, cols, stage),
                (args.batch_id, f["s3_key"]),
            )
            loaded = cur.rowcount

            cur.execute(
                "INSERT INTO meta.lineage (batch_id, layer, source, target, rows_in, rows_out) "
                "VALUES (%s, 'bronze', %s, %s, %s, %s)",
                (args.batch_id, f["s3_key"], f"bronze.{table}", f["rows"], loaded),
            )
            failing = abs(loaded - f["rows"])
            cur.execute(
                "INSERT INTO meta.dq_results (batch_id, layer, check_name, target, severity, failing_rows, sample) "
                "VALUES (%s, 'bronze', 'row_count_matches_manifest', %s, 'block', %s, %s)",
                (args.batch_id, f"bronze.{table}", failing,
                 json.dumps({"manifest_rows": f["rows"], "loaded_rows": loaded})),
            )
            status = "ok" if failing == 0 else "FAIL"
            if failing:
                failures.append(f"bronze.{table}: manifest {f['rows']} vs loaded {loaded}")
            print(f"bronze.{table:25} {f['rows']:>9} {loaded:>9}  {status}")

        if failures:
            conn.rollback()
            raise SystemExit("block check failed, bronze rolled back:\n  " + "\n  ".join(failures))
        conn.commit()
    print(f"\nbronze batch {args.batch_id} committed")


if __name__ == "__main__":
    main()
