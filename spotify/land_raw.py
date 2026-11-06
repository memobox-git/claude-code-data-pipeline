"""Step 1: land dataset.csv in the raw zone exactly as downloaded, with a manifest.

The manifest records bytes, sha256, header and the real CSV row count. Bronze
loads from the manifest and checks it loaded exactly that many rows.
"""
from __future__ import annotations

import csv
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from .common import DATASET_REF, RawStore

csv.field_size_limit(sys.maxsize)


def describe(path: Path) -> dict:
    sha = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        header = next(reader)
        rows = sum(1 for _ in reader)
    return {"file": path.name, "bytes": path.stat().st_size, "sha256": sha.hexdigest(),
            "columns": header, "rows": rows}


def land(folder: Path, batch_id: str | None = None) -> str:
    batch_id = batch_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    files = sorted(folder.glob("*.csv"))
    if not files:
        raise SystemExit(f"no CSV files in {folder}")

    store = RawStore(batch_id)
    manifest = {"batch_id": batch_id, "source": f"kaggle:{DATASET_REF}",
                "landed_at": datetime.now(timezone.utc).isoformat(), "files": []}
    print(f"{'file':20} {'rows':>9} {'bytes':>11}")
    for path in files:
        info = describe(path)
        info["key"] = store.put_file(path)
        manifest["files"].append(info)
        print(f"{info['file']:20} {info['rows']:>9} {info['bytes']:>11}")
    store.put_json("_manifest.json", manifest)
    print(f"\nbatch {batch_id}: {len(files)} file(s) landed under {store.where()}")
    return batch_id
