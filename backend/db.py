"""Postgres connections for the API. Schema is Alembic-only — no DDL here.

Never log the connection URL (it embeds a password). Placeholders are %s.
prepare_threshold=0 so PgBouncer/Supabase poolers accept the session.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from .db_url import database_url, psycopg_url

IntegrityError = UniqueViolation


def require_database_url() -> str:
    raw = database_url()
    if not raw:
        raise RuntimeError(
            "DATABASE_URL (or SUPABASE_DB_URL) is not set. "
            "Postgres is required at runtime; SQLite is no longer used."
        )
    return psycopg_url(raw)


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(
        require_database_url(),
        row_factory=dict_row,
        prepare_threshold=0,
    )
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> None:
    with connection() as conn:
        conn.execute("SELECT 1")
