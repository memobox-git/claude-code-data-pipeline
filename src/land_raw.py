"""Step 1: land the Olist CSV files in the S3 raw zone, exactly as downloaded.

Usage:
    python src/land_raw.py                        # download from Kaggle, then land
    python src/land_raw.py --folder data/olist_raw  # land CSVs already on disk
    python src/land_raw.py --batch-id 20261106T120000Z

Writes  s3://$S3_BUCKET/raw/olist/batch=<batch_id>/<file>.csv   (bytes unchanged)
and     s3://$S3_BUCKET/raw/olist/batch=<batch_id>/_manifest.json
with bytes, sha256, columns and the real data row count per file (multi-line
quoted fields are counted as one row). S3 verifies each upload against the
local sha256, so a file that lands is byte-identical to the download.

The bronze step loads from the manifest and records this step's
source -> target row counts in meta.lineage when it runs inside the Lambda;
the sandbox itself cannot reach the database.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from common import ROOT, aws_session, settings

DATASET = "olistbr/brazilian-ecommerce"
EXPECTED_FILES = 9
DOWNLOAD_DIR = ROOT / "data" / "olist_raw"      # git-ignored; holds nothing but the download
MANIFEST_DIR = ROOT / "data" / "landed"          # git-ignored local copy of each manifest

csv.field_size_limit(sys.maxsize)


def download(folder: Path) -> None:
    """Fetch the dataset with the Kaggle CLI into an empty folder."""
    folder.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    if not env.get("KAGGLE_API_TOKEN") and not (Path.home() / ".kaggle" / "kaggle.json").exists():
        env["KAGGLE_API_TOKEN"] = "proxy-injected"   # cloud session: the proxy swaps in the real token
    kaggle = shutil.which("kaggle") or str(Path(sys.executable).parent / "kaggle")
    subprocess.run(
        [kaggle, "datasets", "download", "-d", DATASET, "-p", str(folder), "--unzip", "--force"],
        check=True, env=env,
    )


def describe(path: Path) -> dict:
    """Bytes, sha256, header columns and real data row count of one CSV."""
    data = path.read_bytes()
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        columns = next(reader)
        rows = sum(1 for _ in reader)
    return {
        "file": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "columns": columns,
        "rows": rows,
    }


def upload(s3, bucket: str, key: str, path: Path, sha256_hex: str) -> None:
    """Single PUT with the local sha256; S3 rejects the object if the bytes differ."""
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=path.read_bytes(),
        ContentType="text/csv",
        ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256_hex)).decode(),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--folder", type=Path, help="CSVs already on disk (skips the Kaggle download)")
    ap.add_argument("--batch-id", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    args = ap.parse_args()

    cfg = settings()
    folder = args.folder or DOWNLOAD_DIR
    if not args.folder:
        download(folder)

    files = sorted(folder.glob("*.csv"))
    if len(files) != EXPECTED_FILES:
        raise SystemExit(f"expected {EXPECTED_FILES} CSV files in {folder}, found {len(files)}")

    prefix = f"raw/olist/batch={args.batch_id}/"
    s3 = aws_session(cfg).client("s3")
    bucket = cfg["S3_BUCKET"]

    entries = []
    for path in files:
        entry = describe(path)
        entry["key"] = prefix + path.name
        upload(s3, bucket, entry["key"], path, entry["sha256"])
        entries.append(entry)
        print(f"  landed {path.name:40} {entry['bytes']:>10,} bytes  {entry['rows']:>9,} rows")

    manifest = {
        "dataset": DATASET,
        "batch_id": args.batch_id,
        "prefix": prefix,
        "landed_by": cfg.get("OWNER", ""),
        "landed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "files": entries,
    }
    body = json.dumps(manifest, indent=1).encode()
    s3.put_object(Bucket=bucket, Key=prefix + "_manifest.json", Body=body, ContentType="application/json")
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    (MANIFEST_DIR / f"{args.batch_id}_manifest.json").write_bytes(body)

    print(f"\nbatch_id={args.batch_id}  files={len(entries)}  "
          f"bytes={sum(e['bytes'] for e in entries):,}  rows={sum(e['rows'] for e in entries):,}")
    print(f"manifest: {prefix}_manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
