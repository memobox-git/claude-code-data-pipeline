"""Step 1: land the Olist CSV files in the S3 raw zone, exactly as downloaded.

Usage:
    python src/land_raw.py <folder-with-csvs> [--batch-id YYYYMMDDTHHMMSSZ]

Writes to  s3://$S3_BUCKET/raw/olist/batch=<batch_id>/<file>.csv
plus       s3://$S3_BUCKET/raw/olist/batch=<batch_id>/_manifest.json
with per-file bytes, sha256, real CSV row count (multi-line quoted fields
counted correctly) and the upload time. The manifest is what bronze loads from.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import PROJECT_TAG, aws_session, settings

csv.field_size_limit(sys.maxsize)


def describe(path: Path) -> dict:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = sum(1 for _ in reader)
    return {
        "file": path.name, "bytes": path.stat().st_size,
        "sha256": sha.hexdigest(), "columns": header, "rows": rows,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", type=Path)
    ap.add_argument("--batch-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = ap.parse_args()

    cfg = settings()
    s3 = aws_session(cfg).client("s3")
    bucket, prefix = cfg["S3_BUCKET"], f"raw/olist/batch={args.batch_id}/"
    tagging = f"{PROJECT_TAG['Key']}={PROJECT_TAG['Value']}"

    files = sorted(args.folder.glob("*.csv"))
    if not files:
        raise SystemExit(f"no CSV files in {args.folder}")

    manifest = {
        "batch_id": args.batch_id, "source": "kaggle:olistbr/brazilian-ecommerce",
        "landed_at": datetime.now(timezone.utc).isoformat(), "files": [],
    }
    print(f"{'file':42} {'rows':>9} {'bytes':>10}")
    for path in files:
        info = describe(path)
        s3.upload_file(str(path), bucket, prefix + path.name, ExtraArgs={"Tagging": tagging})
        info["s3_key"] = prefix + path.name
        manifest["files"].append(info)
        print(f"{info['file']:42} {info['rows']:>9} {info['bytes']:>10}")

    s3.put_object(
        Bucket=bucket, Key=prefix + "_manifest.json", Tagging=tagging,
        Body=json.dumps(manifest, indent=2).encode(), ContentType="application/json",
    )
    print(f"\nbatch {args.batch_id}: {len(files)} files landed under s3://<bucket>/{prefix}")


if __name__ == "__main__":
    main()
