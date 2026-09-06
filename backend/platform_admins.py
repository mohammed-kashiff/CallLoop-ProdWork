"""DB-managed additions to the platform-admin allowlist (0026).

Platform admin is CallLoop-internal staff only — it must never be
reachable by a customer. The static PLATFORM_ADMIN_EMAILS env var
(auth.is_platform_admin()) stays the permanent bedrock; this module is
purely additive, letting an existing platform admin grant it to someone
else from Command Center without a Render env change + restart.

Every function here calls one of the four SECURITY DEFINER SQL functions
from migration 0026 (is_platform_admin_email / list_platform_admins /
add_platform_admin / remove_platform_admin) — platform_admins itself is
never granted to callproof_app, so a plain SELECT/INSERT/DELETE against
the table would fail even with a bug in this file. No org_scope() here:
platform admin is cross-tenant by definition, there is no org_id to
scope to.

Callers must already have run auth.require_platform_admin(request) —
these functions do no permission check of their own, same convention as
admin_console.py's own entrypoints.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import db

log = logging.getLogger("callproof.platform_admins")


def _normalize_email(email: str | None) -> str:
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized or len(normalized) > 320:
        raise HTTPException(status_code=400, detail="A valid email address is required.")
    return normalized


def list_platform_admins() -> list[dict]:
    """Every email granted platform admin through this table — does NOT
    include PLATFORM_ADMIN_EMAILS entries, since those were never
    inserted here. The UI should say so explicitly."""
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM public.list_platform_admins()").fetchall()
    return [
        {
            "email": r["email"],
            "added_by": r["added_by"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        }
        for r in rows or []
    ]


def add_platform_admin(email: str, *, added_by: str | None) -> dict:
    normalized = _normalize_email(email)
    with db.connection() as conn:
        conn.execute(
            "SELECT public.add_platform_admin(%s, %s)", (normalized, added_by),
        )
    applog.event(log, "platform_admin_added", email=normalized, added_by=added_by)
    return {"email": normalized, "added_by": added_by}


def remove_platform_admin(email: str) -> None:
    normalized = _normalize_email(email)
    with db.connection() as conn:
        conn.execute("SELECT public.remove_platform_admin(%s)", (normalized,))
    applog.event(log, "platform_admin_removed", email=normalized)
