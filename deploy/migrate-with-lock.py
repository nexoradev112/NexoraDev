"""Serialize schema migrations across API starts with a PostgreSQL advisory lock."""

from __future__ import annotations

import os
import subprocess
import sys

import psycopg


LOCK_ID = 6_162_550_742_640_001


def main() -> int:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required before migrations can run")

    psycopg_url = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(psycopg_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            return subprocess.run(
                [sys.executable, "-m", "app.cli", "migrate"],
                check=False,
            ).returncode
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))


if __name__ == "__main__":
    raise SystemExit(main())
