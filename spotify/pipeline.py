"""Run the Spotify pipeline.

Usage (from the repo root):
    python -m spotify.pipeline all data/spotify          # land -> bronze -> silver -> gold -> role
    python -m spotify.pipeline land data/spotify [--batch-id ID]
    python -m spotify.pipeline bronze --batch-id ID
    python -m spotify.pipeline silver [--batch-id ID]    # default: latest batch
    python -m spotify.pipeline gold   [--batch-id ID]
    python -m spotify.pipeline role                      # (re)apply the agent's read-only grants
"""
from __future__ import annotations

import argparse
from pathlib import Path

from .common import admin_connect


def latest_batch() -> str:
    with admin_connect() as conn:
        row = conn.execute("SELECT batch_id FROM meta.batch ORDER BY registered_at DESC LIMIT 1").fetchone()
    if not row:
        raise SystemExit("no batch loaded yet: run land and bronze first")
    return row[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["land", "bronze", "silver", "gold", "role", "all"])
    ap.add_argument("folder", nargs="?", type=Path, help="folder with dataset.csv (land, all)")
    ap.add_argument("--batch-id")
    a = ap.parse_args()

    if a.step in ("land", "all"):
        if not a.folder:
            ap.error(f"{a.step} needs the folder that holds dataset.csv")
        from .land_raw import land
        a.batch_id = land(a.folder, a.batch_id)
    if a.step in ("bronze", "all"):
        if not a.batch_id:
            ap.error("bronze needs --batch-id")
        from .load_bronze import run as load_bronze
        load_bronze(a.batch_id)
    if a.step in ("silver", "gold", "all"):
        from .build_layer import run as build_layer
        batch_id = a.batch_id or latest_batch()
        for layer in (["silver", "gold"] if a.step == "all" else [a.step]):
            build_layer(layer, batch_id)
    if a.step in ("role", "all"):
        from .agent_role import setup
        setup()


if __name__ == "__main__":
    main()
