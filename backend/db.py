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

# NOLOGIN / NOBYPASSRLS role created in Alembic 0005_rls. API SET LOCAL ROLE
# so RLS applies even when DATABASE_URL is the postgres URI.
APP_ROLE = "callproof_app"


def require_database_url() -> str:
    raw = database_url()
    if not raw:
        raise RuntimeError(
            "DATABASE_URL (or SUPABASE_DB_URL) is not set. "
            "Postgres is required at runtime; SQLite is no longer used."
        )
    return psycopg_url(raw)


def apply_tenant_gucs(
    conn,
    *,
    org_id: str | None = None,
    user_id: str | None = None,
) -> None:
    """SET LOCAL ROLE callproof_app, then app.current_org_id / app.current_user_id.

    Values come from the JWT-bound context, never from a query string or body.
    Empty string means unset (policies deny). Parameterized — not concatenated.
    is_local=true so GUCs do not leak across pooled connections; re-applied after
    COMMIT/ROLLBACK because SET LOCAL is transaction-scoped.
    SET ROLE is what makes RLS apply when DATABASE_URL is postgres.
    """
    from .org_ids import bound_org_id, bound_user_id, parse_org_id

    oid = bound_org_id() if org_id is None else parse_org_id(org_id)
    uid = bound_user_id() if user_id is None else parse_org_id(user_id)
    # Identifier is a constant we own. SET LOCAL so COMMIT/ROLLBACK drop it;
    # _RlsConnection re-applies. This is what stops postgres BYPASSRLS.
    conn.execute("SELECT set_config(%s, %s, true)", ("role", APP_ROLE))
    conn.execute(
        "SELECT set_config(%s, %s, true)",
        ("app.current_org_id", oid or ""),
    )
    conn.execute(
        "SELECT set_config(%s, %s, true)",
        ("app.current_user_id", uid or ""),
    )


class _RlsConnection:
    """Proxy that restores tenant GUCs after COMMIT/ROLLBACK."""

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def commit(self) -> None:
        self._conn.commit()
        apply_tenant_gucs(self._conn)

    def rollback(self) -> None:
        self._conn.rollback()
        apply_tenant_gucs(self._conn)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)


@contextmanager
def connection(*, bypass_rls: bool = False) -> Iterator[psycopg.Connection]:
    """Open a connection and bind tenant GUCs for RLS.

    bypass_rls=True skips SET LOCAL ROLE callproof_app and tenant GUCs.
    Use only from Alembic-adjacent backfill running as postgres/service_role.
    The API must never pass True.
    """
    conn = psycopg.connect(
        require_database_url(),
        row_factory=dict_row,
        prepare_threshold=0,
    )
    wrapped = conn if bypass_rls else _RlsConnection(conn)
    try:
        if not bypass_rls:
            apply_tenant_gucs(conn)
        yield wrapped
        wrapped.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ping() -> None:
    with connection() as conn:
        conn.execute("SELECT 1")
