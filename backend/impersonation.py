"""Platform-admin "log in as": mint a real Supabase session for an org
member so an admin can view/act as that customer from Command Center,
without ever touching their password (CC-1).

Admin-only, no live consent step (explicit product decision) — the
impersonation_log row is the accountability mechanism, not a gate. Every
call assumes require_platform_admin already ran, same convention as
admin_console.py.

Uses the Supabase Auth Admin REST API directly via httpx, matching the
existing pattern in admin_provision.py rather than adding the supabase-py
SDK as a new dependency. Two calls: admin/generate_link mints a one-time
magic-link token for the target user (email-based — org_members has no
password of its own to touch), then /verify exchanges that token_hash for
a real session server-side, so the frontend never has to redirect through
Supabase's own hosted verify page.

Smoke-tested end-to-end against the real project (2026-09-04): hashed_token
comes back top-level (merged into the full user object), not nested under
a "properties" key — Supabase's public docs don't pin this down precisely,
so _extract_hashed_token still checks both defensively, but the shape that
actually matters here is confirmed, not assumed.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException

from . import applog
from . import db
from .admin_console import search_directory
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.impersonation")

_HTTP_TIMEOUT = 15.0


def _supabase_admin() -> tuple[str, dict[str, str]]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth is not configured")
    return url, {"apikey": key, "Authorization": f"Bearer {key}"}


def _directory_row_for_user(user_id: str) -> dict | None:
    """Exact user_id match from the admin directory (email, org_id, org_name,
    role). Reuses admin_search_directory rather than a new SQL surface —
    it already matches a substring of user_id, so the id itself is a valid
    query string."""
    result = search_directory(user_id)
    for row in result.get("rows", []) or []:
        if str(row.get("user_id")) == user_id:
            return row
    return None


def _extract_hashed_token(payload: dict) -> str | None:
    props = payload.get("properties")
    if isinstance(props, dict) and props.get("hashed_token"):
        return props["hashed_token"]
    return payload.get("hashed_token")


def _generate_link(email: str) -> str:
    url, headers = _supabase_admin()
    try:
        resp = httpx.post(
            f"{url}/auth/v1/admin/generate_link",
            headers=headers,
            json={"type": "magiclink", "email": email},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        applog.event(
            log, "impersonation_link_failed", level=logging.ERROR,
            error=applog.safe_exception_text(e),
        )
        raise HTTPException(status_code=502, detail="Could not start impersonation session.")
    hashed_token = _extract_hashed_token(resp.json() or {})
    if not hashed_token:
        applog.event(
            log, "impersonation_link_shape_unexpected", level=logging.ERROR,
        )
        raise HTTPException(status_code=502, detail="Impersonation session was not issued.")
    return hashed_token


def _verify(hashed_token: str) -> dict:
    """/verify only needs a valid project apikey header (any project key
    satisfies Supabase's gateway check here, not anon-only) — reuses the
    same service-role credential as generate_link rather than requiring a
    second env var this codebase has never otherwise needed."""
    url, headers = _supabase_admin()
    try:
        resp = httpx.post(
            f"{url}/auth/v1/verify",
            headers={"apikey": headers["apikey"], "Content-Type": "application/json"},
            json={"type": "magiclink", "token_hash": hashed_token},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
    except httpx.HTTPError as e:
        applog.event(
            log, "impersonation_verify_failed", level=logging.ERROR,
            error=applog.safe_exception_text(e),
        )
        raise HTTPException(status_code=502, detail="Could not start impersonation session.")
    session = resp.json() or {}
    if not session.get("access_token"):
        raise HTTPException(status_code=502, detail="Impersonation session was not issued.")
    return session


def _record(
    *, org_id: str, admin_email: str, target_user_id: str, target_email: str,
    ip_address: str | None,
) -> None:
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO impersonation_log (
                    org_id, admin_email, target_user_id, target_email, ip_address
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (org_id, admin_email, target_user_id, target_email, ip_address),
            )
    applog.event(
        log, "impersonation_started",
        org_id=org_id, admin_email=admin_email, target_user_id=target_user_id,
    )


def start_impersonation(
    user_id: str, *, admin_email: str, ip_address: str | None = None,
) -> dict:
    """Mint a real session for this org member. Caller must have already
    run require_platform_admin — this function does not check it again."""
    row = _directory_row_for_user(user_id)
    if not row or not row.get("email"):
        raise HTTPException(status_code=404, detail="No such user to log in as.")
    org_id = parse_org_id(row.get("org_id"))
    if not org_id:
        raise HTTPException(status_code=404, detail="This user has no org membership.")
    target_email = row["email"]

    hashed_token = _generate_link(target_email)
    session = _verify(hashed_token)

    _record(
        org_id=org_id, admin_email=admin_email, target_user_id=user_id,
        target_email=target_email, ip_address=ip_address,
    )

    return {
        "org_id": org_id,
        "org_name": row.get("org_name"),
        "target_email": target_email,
        "access_token": session.get("access_token"),
        "refresh_token": session.get("refresh_token"),
        "expires_in": session.get("expires_in"),
        "token_type": session.get("token_type") or "bearer",
    }
