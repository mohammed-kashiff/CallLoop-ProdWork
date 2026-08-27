"""Platform-admin user provisioning (AC-3).

Creates a Supabase Auth user with a generated password and an org_members
row. Callers must already have passed require_platform_admin.

org_id in the body (org_mode=existing) is the TARGET tenant to join, not the
caller's JWT org. This is the platform-admin exception to tenant isolation.
Do not copy a body org_id into ordinary /api/* handlers. RLS GUC is switched
to that org for the insert — same pattern as ensure_membership's personal-org
branch. Never bypass_rls.

The generated password is returned once in the HTTP response. It is not
written to the database and must never be logged.
"""

from __future__ import annotations

import logging
import os
import secrets
import uuid

import httpx
from fastapi import HTTPException

from . import applog
from . import audit_store
from . import auth
from . import db
from .org_ids import DEFAULT_ORG_ID, parse_org_id

log = logging.getLogger("callproof.admin_provision")

_HTTP_TIMEOUT = 30.0


def provision_user(
    *,
    email: str,
    first_name: str,
    last_name: str,
    org_mode: str,
    org_id: str | None = None,
    org_name: str | None = None,
) -> dict:
    email_s = (email or "").strip().lower()
    first = auth._optional_name(first_name)
    last = auth._optional_name(last_name)
    mode = (org_mode or "").strip().lower()

    if auth._signup_domain(email_s) is None:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if not first or not last:
        raise HTTPException(status_code=400, detail="First and last name are required.")
    if mode not in ("new", "existing"):
        raise HTTPException(status_code=400, detail="org_mode must be new or existing.")

    target_org: str | None = None
    existing_name: str | None = None
    if mode == "existing":
        target_org = parse_org_id(org_id)
        if not target_org:
            raise HTTPException(status_code=400, detail="org_id is required.")
        if target_org == DEFAULT_ORG_ID:
            raise HTTPException(status_code=400, detail="Cannot provision into the placeholder org.")
        existing_name = _lookup_org_name(target_org)
        if existing_name is None:
            raise HTTPException(status_code=404, detail="Organization not found.")

    password = secrets.token_urlsafe(16)
    user_id = _create_auth_user(
        email=email_s,
        password=password,
        first_name=first,
        last_name=last,
    )
    requested_name = (org_name or "").strip()[:120] or None
    try:
        if mode == "new":
            out_org_id, out_org_name, role = _insert_new_org(
                user_id=user_id,
                email=email_s,
                first_name=first,
                last_name=last,
                org_name=requested_name,
            )
        else:
            assert target_org is not None
            out_org_id, out_org_name, role = _insert_existing_member(
                org_id=target_org,
                org_name=existing_name or "",
                user_id=user_id,
                first_name=first,
                last_name=last,
            )
    except HTTPException:
        _delete_auth_user(user_id)
        raise
    except Exception:
        _delete_auth_user(user_id)
        raise HTTPException(status_code=500, detail="Could not complete provisioning.")

    applog.event(
        log,
        "admin_user_provisioned",
        org_mode=mode,
        org_id=out_org_id,
        user_id=user_id,
        role=role,
    )
    return {
        "email": email_s,
        "user_id": user_id,
        "org_id": out_org_id,
        "org_name": out_org_name,
        "temporary_password": password,
    }


def _supabase_admin() -> tuple[str, dict[str, str]]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth is not configured")
    return url, {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _create_auth_user(
    *,
    email: str,
    password: str,
    first_name: str,
    last_name: str,
) -> str:
    url, headers = _supabase_admin()
    try:
        response = httpx.post(
            f"{url}/auth/v1/admin/users",
            headers=headers,
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {
                    "first_name": first_name,
                    "last_name": last_name,
                },
            },
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not create the user.") from None
    if response.status_code in (200, 201):
        try:
            body = response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Could not create the user.") from None
        user_id = parse_org_id((body or {}).get("id") if isinstance(body, dict) else None)
        if not user_id:
            raise HTTPException(status_code=502, detail="Could not create the user.")
        return user_id
    if response.status_code in (409, 422):
        raise HTTPException(status_code=409, detail="That email is already registered.")
    raise HTTPException(status_code=502, detail="Could not create the user.")


def _delete_auth_user(user_id: str) -> None:
    try:
        url, headers = _supabase_admin()
        httpx.delete(
            f"{url}/auth/v1/admin/users/{user_id}",
            headers=headers,
            timeout=_HTTP_TIMEOUT,
        )
    except Exception:
        applog.event(
            log,
            "admin_provision_rollback_failed",
            level=logging.ERROR,
            user_id=user_id,
        )


def _lookup_org_name(org_id: str) -> str | None:
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=org_id)
        row = conn.execute(
            "SELECT id, name FROM orgs WHERE id = %s",
            (org_id,),
        ).fetchone()
    if not row:
        return None
    return str(row.get("name") or "")


def _insert_new_org(
    *,
    user_id: str,
    email: str,
    first_name: str,
    last_name: str,
    org_name: str | None = None,
) -> tuple[str, str, str]:
    org_id = str(uuid.uuid4())
    if org_id == DEFAULT_ORG_ID:
        org_id = str(uuid.uuid4())
    org_name = org_name or auth._workspace_name(email)
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=org_id, user_id=user_id)
        conn.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s)",
            (org_id, org_name),
        )
        audit_store.seed_legacy_rubric(
            conn, org_id=org_id, rubric_id=str(uuid.uuid4()),
        )
        conn.execute(
            """
            INSERT INTO org_members (
                org_id, user_id, role, first_name, last_name
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (org_id, user_id, "owner", first_name, last_name),
        )
    return org_id, org_name, "owner"


def _insert_existing_member(
    *,
    org_id: str,
    org_name: str,
    user_id: str,
    first_name: str,
    last_name: str,
) -> tuple[str, str, str]:
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=org_id, user_id=user_id)
        try:
            conn.execute(
                """
                INSERT INTO org_members (
                    org_id, user_id, role, first_name, last_name
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (org_id, user_id, "member", first_name, last_name),
            )
        except db.IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="That user already belongs to an organization.",
            ) from exc
    return org_id, org_name, "member"
