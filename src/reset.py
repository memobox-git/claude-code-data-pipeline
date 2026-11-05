"""Demo reset: empty the database layers and the raw zone so the run can be replayed.
Keeps the RDS instance, the bucket, roles and the Lambda. Only for the live demo.
"""
from __future__ import annotations

from common import aws_session, db_connect, settings


def run(_batch_id: str | None = None) -> dict:
    cfg = settings()
    session = aws_session(cfg)
    with db_connect(cfg, session) as conn:
        cur = conn.cursor()
        for schema in ("gold", "silver", "bronze", "meta"):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.commit()
        cur.execute("SELECT schema_name FROM information_schema.schemata WHERE schema_name IN ('bronze','silver','gold','meta')")
        left = [r[0] for r in cur.fetchall()]

    s3 = session.client("s3")
    deleted = 0
    for prefix in ("raw/", "gold-snapshots/"):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=cfg["S3_BUCKET"], Prefix=prefix):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=cfg["S3_BUCKET"], Delete={"Objects": keys, "Quiet": True})
                deleted += len(keys)
    return {"schemas_left": left, "s3_objects_deleted": deleted}
