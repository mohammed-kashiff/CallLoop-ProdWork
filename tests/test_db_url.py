"""DATABASE_URL helpers must not require a live database."""

from backend.db_url import database_url, sqlalchemy_url


def test_sqlalchemy_url_rewrites_heroku_scheme():
    out = sqlalchemy_url("postgres://user:secret@db.example:5432/app")
    assert out.startswith("postgresql+psycopg://")
    assert "db.example" in out
    assert "secret" in out


def test_sqlalchemy_url_idempotent_psycopg():
    raw = "postgresql+psycopg://u:p@localhost/db"
    assert sqlalchemy_url(raw) == raw


def test_database_url_empty(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    assert database_url() is None
