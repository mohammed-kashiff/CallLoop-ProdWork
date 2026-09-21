"""Owner invite: Supabase Auth invite email, then org_members as member.

JWT org_id only — never a body org_id. No password is generated, returned,
or logged. Do not copy admin_provision's platform-admin target-org exception.
"""

from __future__ import annotations

import logging
import os

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import audit_log
from . import auth
from . import db
from . import rate_limit
from .config import CUSTOMER_ORIGIN
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.org_invite")

_HTTP_TIMEOUT = 30.0
_REDIRECT = f"{CUSTOMER_ORIGIN}/reset-password"


class InviteBody(BaseModel):
    email: str
    first_name: str
    last_name: str


def invite_route(request: Request, body: InviteBody):
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    rate_limit.enforce("team_invite", org_id, limit=10, window_seconds=3600)
    return invite_member(
        org_id=org_id,
        email=body.email,
        first_name=body.first_name,
        last_name=body.last_name,
    )


def invite_member(*, org_id: str, email: str, first_name: str, last_name: str) -> dict:
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    email_s = (email or "").strip().lower()
    first = auth._optional_name(first_name)
    last = auth._optional_name(last_name)
    if auth._signup_domain(email_s) is None:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if not first or not last:
        raise HTTPException(status_code=400, detail="First and last name are required.")

    existing_uid = _lookup_auth_user_id(email_s)
    if existing_uid:
        home = _membership_org(existing_uid)
        if home == oid:
            raise HTTPException(status_code=409, detail="That person is already on this team.")
        if home:
            raise HTTPException(
                status_code=409, detail="That user already belongs to an organization.",
            )
        raise HTTPException(status_code=409, detail="That email is already registered.")

    user_id = _invite_auth_user(email=email_s, first_name=first, last_name=last)
    try:
        _insert_member(org_id=oid, user_id=user_id, first_name=first, last_name=last)
    except HTTPException:
        _delete_auth_user(user_id)
        raise
    except Exception:
        _delete_auth_user(user_id)
        raise HTTPException(status_code=500, detail="Could not complete the invite.") from None

    audit_log.record(
        oid, "member.invited",
        target_type="org_member", target_id=user_id,
        after={"email": email_s, "role": "member"},
    )
    applog.event(log, "member_invited", org_id=oid, user_id=user_id, role="member")
    return {"email": email_s, "user_id": user_id, "role": "member"}


def _supabase_admin() -> tuple[str, dict[str, str]]:
    url = (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()
    if not url or not key:
        raise HTTPException(status_code=503, detail="Auth is not configured")
    return url, {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }


def _lookup_auth_user_id(email: str) -> str | None:
    url, headers = _supabase_admin()
    try:
        response = httpx.get(
            f"{url}/auth/v1/admin/users",
            headers=headers,
            params={"email": email, "per_page": 1},
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not look up that email.") from None
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    users = body.get("users") if isinstance(body, dict) else None
    if not isinstance(users, list) or not users:
        return None
    return parse_org_id(users[0].get("id") if isinstance(users[0], dict) else None)


def _membership_org(user_id: str) -> str | None:
    with db.connection() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE user_id = %s",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    return parse_org_id(row.get("org_id"))


def _invite_auth_user(*, email: str, first_name: str, last_name: str) -> str:
    url, headers = _supabase_admin()
    try:
        response = httpx.post(
            f"{url}/auth/v1/invite",
            headers=headers,
            json={
                "email": email,
                "data": {"first_name": first_name, "last_name": last_name},
                "redirect_to": _REDIRECT,
            },
            timeout=_HTTP_TIMEOUT,
        )
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="Could not send the invite.") from None
    if response.status_code in (200, 201):
        try:
            body = response.json()
        except ValueError:
            raise HTTPException(status_code=502, detail="Could not send the invite.") from None
        user = body.get("user") if isinstance(body, dict) and isinstance(body.get("user"), dict) else body
        user_id = parse_org_id((user or {}).get("id") if isinstance(user, dict) else None)
        if not user_id:
            raise HTTPException(status_code=502, detail="Could not send the invite.")
        return user_id
    if response.status_code in (409, 422):
        raise HTTPException(status_code=409, detail="That email is already registered.")
    raise HTTPException(status_code=502, detail="Could not send the invite.")


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
            "org_invite_rollback_failed",
            level=logging.ERROR,
            user_id=user_id,
        )


def _insert_member(*, org_id: str, user_id: str, first_name: str, last_name: str) -> None:
    with org_scope(org_id):
        with db.connection() as conn:
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


def register(app) -> None:
    app.add_api_route("/api/team/invite", invite_route, methods=["POST"])
