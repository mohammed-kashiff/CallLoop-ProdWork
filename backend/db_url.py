"""Read the Postgres URL from the environment. Never log the value (it embeds a password)."""

from __future__ import annotations

import os


def database_url() -> str | None:
    raw = (os.getenv("DATABASE_URL") or os.getenv("SUPABASE_DB_URL") or "").strip()
    return raw or None


def sqlalchemy_url(raw: str) -> str:
    """SQLAlchemy 2 wants postgresql:// (or +psycopg). Heroku-style postgres:// still appears."""
    url = raw.strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and "+psycopg" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


def psycopg_url(raw: str) -> str:
    """psycopg.connect wants postgresql://, not SQLAlchemy's +psycopg driver tag."""
    url = raw.strip()
    if url.startswith("postgresql+psycopg://"):
        return "postgresql://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://") :]
    return url
