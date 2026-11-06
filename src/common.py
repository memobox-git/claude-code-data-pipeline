"""Shared helpers: settings, AWS session, database connection.

Settings come from config.env (laptop or cloud session) or from environment
variables with the same names (inside the Lambda). Secrets live in neither:
the database password is fetched from Secrets Manager at runtime and is never
printed, logged or saved.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import boto3
import botocore.session
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.env"

REQUIRED = ("AWS_REGION", "S3_BUCKET", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_SECRET_NAME")
OPTIONAL = ("AWS_PROFILE", "OWNER", "BI_USER")


def settings() -> dict[str, str]:
    """Values from config.env, overridden by environment variables of the same name."""
    cfg: dict[str, str] = {}
    if CONFIG_FILE.exists():
        cfg = {k: v for k, v in dotenv_values(CONFIG_FILE).items() if v}
    for key in REQUIRED + OPTIONAL:
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    cfg.setdefault("AWS_REGION", "us-east-1")
    cfg.setdefault("BI_USER", "bi_reader")
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        raise SystemExit(f"missing settings: {', '.join(missing)} (copy config.env.example to config.env)")
    return cfg


def aws_session(cfg: dict[str, str]) -> boto3.Session:
    """Use AWS_PROFILE only when that profile exists on this machine.

    In a cloud session or inside the Lambda there is no profile file; the keys
    arrive through the environment and the default credential chain finds them.
    """
    profile = cfg.get("AWS_PROFILE") or None
    if profile and profile not in botocore.session.Session().available_profiles:
        profile = None
    return boto3.Session(profile_name=profile, region_name=cfg["AWS_REGION"])


def db_password(cfg: dict[str, str], session: boto3.Session) -> str:
    """Fetch the database password from Secrets Manager. Never print or log it."""
    raw = session.client("secretsmanager").get_secret_value(SecretId=cfg["DB_SECRET_NAME"])["SecretString"]
    return json.loads(raw)["password"]


def db_connect(cfg: dict[str, str], session: boto3.Session, autocommit: bool = False):
    """Open a psycopg connection to the RDS instance (only reachable from inside AWS)."""
    import psycopg  # imported here so steps that never touch the database do not need it

    return psycopg.connect(
        host=cfg["DB_HOST"],
        port=int(cfg["DB_PORT"]),
        dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"],
        password=db_password(cfg, session),
        sslmode="require",
        connect_timeout=15,
        autocommit=autocommit,
    )
