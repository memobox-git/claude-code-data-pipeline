"""Create or update the agent's read-only login, agent_reader (SELECT on gold, nothing else).

On AWS the password comes from Secrets Manager (AGENT_SECRET_NAME, a JSON secret with a
"password" key). It is sent to Postgres as a SCRAM-SHA-256 verifier, so the plain password
never appears in SQL text or server logs. Locally (SPOTIFY_AGENT_DATABASE_URL set) no
password is set; the local server trusts its Unix socket.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from psycopg import sql

from .common import SQL_DIR, admin_connect, env, secret_password


def scram_sha256(password: str, iterations: int = 4096) -> str:
    """The verifier Postgres stores for a SCRAM-SHA-256 password (RFC 5802 / 7677)."""
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"SCRAM-SHA-256${iterations}:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"


def setup() -> None:
    with admin_connect() as conn:
        cur = conn.cursor()
        cur.execute((SQL_DIR / "40_agent_role.sql").read_text())
        secret = env("AGENT_SECRET_NAME")
        if secret and not env("SPOTIFY_AGENT_DATABASE_URL"):
            verifier = scram_sha256(secret_password(secret))
            cur.execute(sql.SQL("ALTER ROLE agent_reader PASSWORD {}").format(sql.Literal(verifier)))
            print("agent_reader: password set from Secrets Manager")
        conn.commit()
    print("agent_reader: SELECT on gold only, read-only sessions, 15 s statement timeout")
