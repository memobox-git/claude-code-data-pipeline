"""Shared helpers: settings, AWS session, database connection.

Settings come from config.env (git-ignored). The database password is fetched
from Secrets Manager at runtime and never written anywhere.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PROJECT_TAG = {"Key": "project", "Value": "olist-lightning"}


def settings() -> dict[str, str]:
    """Load config.env and return the values the pipeline needs."""
    load_dotenv(ROOT / "config.env", override=False)
    keys = [
        "AWS_REGION", "S3_BUCKET", "DB_HOST", "DB_PORT", "DB_NAME",
        "DB_USER", "DB_SECRET_NAME", "BI_USER", "OWNER",
    ]
    cfg = {k: os.environ.get(k, "") for k in keys}
    missing = [k for k in keys if not cfg[k] and k != "OWNER"]
    if missing:
        raise SystemExit(f"config.env is missing: {', '.join(missing)}")
    return cfg


def aws_session(cfg: dict[str, str]) -> boto3.Session:
    """Use AWS_PROFILE when that profile exists; otherwise fall back to
    whatever credentials the environment provides (keys in env vars)."""
    profile = os.environ.pop("AWS_PROFILE", None)
    if profile and profile in boto3.Session().available_profiles:
        return boto3.Session(profile_name=profile, region_name=cfg["AWS_REGION"])
    return boto3.Session(region_name=cfg["AWS_REGION"])


def db_password(session: boto3.Session, secret_name: str) -> str:
    """Fetch the master password from Secrets Manager. Never log the result."""
    sm = session.client("secretsmanager")
    secret = json.loads(sm.get_secret_value(SecretId=secret_name)["SecretString"])
    return secret["password"]


def db_connect(cfg: dict[str, str], session: boto3.Session):
    import psycopg

    return psycopg.connect(
        host=cfg["DB_HOST"], port=int(cfg["DB_PORT"]), dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"], password=db_password(session, cfg["DB_SECRET_NAME"]),
        sslmode="require", connect_timeout=15,
    )
