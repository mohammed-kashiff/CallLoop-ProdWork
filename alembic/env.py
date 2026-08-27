"""Alembic env. URL comes from DATABASE_URL or SUPABASE_DB_URL — never from this file."""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

from backend.db_url import database_url, sqlalchemy_url
from backend.paths import ENV_FILE

# Local .env must win over a stale DATABASE_URL exported in the shell.
load_dotenv(ENV_FILE, override=True)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

raw = database_url()
if raw:
    # ConfigParser treats % as interpolation — passwords often contain %.
    config.set_main_option("sqlalchemy.url", sqlalchemy_url(raw).replace("%", "%%"))


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    if not database_url():
        raise RuntimeError(
            "DATABASE_URL (or SUPABASE_DB_URL) is not set. "
            "Postgres is required. There is no SQLite fallback."
        )
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
