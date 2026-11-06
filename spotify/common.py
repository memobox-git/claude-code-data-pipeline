"""Shared helpers for the Spotify pipeline and agent: settings, connections, raw store.

Local development: set SPOTIFY_DATABASE_URL (pipeline, writes) and
SPOTIFY_AGENT_DATABASE_URL (agent, read-only role) to any Postgres.

AWS: leave those unset. config.env supplies DB_HOST etc.; passwords are fetched
from Secrets Manager at runtime (DB_SECRET_NAME for the pipeline, AGENT_SECRET_NAME
for the agent) and never written anywhere.

Raw zone: SPOTIFY_RAW=s3 lands files in s3://$S3_BUCKET/raw/spotify/; any other
value is a local folder (default data/, which is git-ignored).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import psycopg
from dotenv import load_dotenv

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
SQL_DIR = PKG / "sql"
DATASET_REF = "maharshipandya/-spotify-tracks-dataset"
TAGGING = "project=olist-lightning"  # CLAUDE.md: tag on every resource

load_dotenv(REPO / "config.env", override=False)


def env(name: str, default: str = "") -> str:
    """A setting from the environment or config.env; empty counts as unset."""
    return os.environ.get(name) or default


def aws_session():
    """Use AWS_PROFILE when that profile exists; otherwise the environment's keys."""
    import boto3

    region = env("AWS_REGION", "us-east-1")
    profile = os.environ.pop("AWS_PROFILE", None)
    if profile and profile in boto3.Session().available_profiles:
        return boto3.Session(profile_name=profile, region_name=region)
    return boto3.Session(region_name=region)


def _secret_password(secret_name: str) -> str:
    """Fetch a password from Secrets Manager. Never log the result."""
    sm = aws_session().client("secretsmanager")
    return json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])["password"]


def _rds_connect(user: str, secret_var: str) -> psycopg.Connection:
    missing = [k for k in ("DB_HOST", secret_var) if not env(k)]
    if missing:
        raise SystemExit(f"config.env is missing: {', '.join(missing)}")
    return psycopg.connect(
        host=env("DB_HOST"), port=int(env("DB_PORT", "5432")), dbname=env("SPOTIFY_DB_NAME", "spotify"),
        user=user, password=_secret_password(env(secret_var)), sslmode="require", connect_timeout=15,
    )


def admin_connect() -> psycopg.Connection:
    """Pipeline connection: builds bronze, silver, gold and meta."""
    url = env("SPOTIFY_DATABASE_URL")
    return psycopg.connect(url, connect_timeout=15) if url else _rds_connect(env("DB_USER", "postgres"), "DB_SECRET_NAME")


def agent_connect() -> psycopg.Connection:
    """Agent connection: logs in as the read-only role (SELECT on gold, nothing else)."""
    url = env("SPOTIFY_AGENT_DATABASE_URL")
    return psycopg.connect(url, connect_timeout=15) if url else _rds_connect(env("AGENT_USER", "agent_reader"), "AGENT_SECRET_NAME")


class RawStore:
    """The raw zone for one batch: files exactly as downloaded, plus a manifest."""

    def __init__(self, batch_id: str):
        self.batch_id = batch_id
        self.prefix = f"raw/spotify/batch={batch_id}/"
        self.kind = env("SPOTIFY_RAW", str(REPO / "data"))  # local files land in data/raw/spotify/...
        if self.kind == "s3":
            self.bucket = env("S3_BUCKET")
            if not self.bucket:
                raise SystemExit("config.env is missing: S3_BUCKET")
            self.s3 = aws_session().client("s3")
        else:
            self.root = Path(self.kind)

    def where(self) -> str:
        """Printable location. Never prints the bucket name."""
        return f"s3://<bucket>/{self.prefix}" if self.kind == "s3" else str(self.root / self.prefix)

    def put_file(self, path: Path) -> str:
        key = self.prefix + path.name
        if self.kind == "s3":
            self.s3.upload_file(str(path), self.bucket, key, ExtraArgs={"Tagging": TAGGING})
        else:
            target = self.root / key
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        return key

    def put_json(self, name: str, obj: dict) -> None:
        body = json.dumps(obj, indent=2).encode()
        if self.kind == "s3":
            self.s3.put_object(Bucket=self.bucket, Key=self.prefix + name, Body=body,
                               Tagging=TAGGING, ContentType="application/json")
        else:
            target = self.root / self.prefix / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)

    def get_json(self, name: str) -> dict:
        if self.kind == "s3":
            return json.loads(self.s3.get_object(Bucket=self.bucket, Key=self.prefix + name)["Body"].read())
        return json.loads((self.root / self.prefix / name).read_text())

    def chunks(self, key: str, size: int = 1 << 20) -> Iterator[bytes]:
        if self.kind == "s3":
            yield from self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].iter_chunks(size)
            return
        with (self.root / key).open("rb") as fh:
            yield from iter(lambda: fh.read(size), b"")
