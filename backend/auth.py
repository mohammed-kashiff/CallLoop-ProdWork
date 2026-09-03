"""Supabase JWT verification and org membership bootstrap (CL-8 / CL-9).

Callers get request.state.user_id / org_id / role from the verified token and
org_members. Handlers must use org_id_from_request — never query, body, or path.
Never log the token.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient
from psycopg.errors import UniqueViolation
from starlette.types import ASGIApp, Receive, Scope, Send

from . import audit_store
from . import db
from .config import cors_origins
from .org_ids import (
    DEFAULT_ORG_ID,
    DEFAULT_RUBRIC_ID,
    bind_org_id,
    bind_user_id,
    parse_org_id,
    reset_org_id,
    reset_user_id,
)

_PUBLIC_EXACT = frozenset({"/", "/health", "/healthz"})
_PUBLIC_PATHS = frozenset({"/api/integrations/justcall/webhook"})
_jwks_client: PyJWKClient | None = None


class AuthError(Exception):
    """Invalid or missing credentials (401)."""


class AuthConfigError(Exception):
    """Server cannot verify tokens (503)."""


@dataclass(frozen=True)
class Membership:
    org_id: str
    role: str
    user_id: str


def _supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")


def _jwt_secret() -> str:
    return (os.getenv("SUPABASE_JWT_SECRET") or "").strip()


def _issuer() -> str:
    url = _supabase_url()
    if not url:
        return ""
    return f"{url}/auth/v1"


def auth_configured() -> bool:
    return bool(_jwt_secret() or _supabase_url())


def _jwks() -> PyJWKClient:
    global _jwks_client
    url = _supabase_url()
    if not url:
        raise AuthConfigError("SUPABASE_URL is not set")
    if _jwks_client is None:
        _jwks_client = PyJWKClient(f"{url}/auth/v1/.well-known/jwks.json")
    return _jwks_client


def verify_access_token(token: str) -> dict:
    """Verify signature, iss, aud, exp. Returns claims. Never logs the token."""
    raw = (token or "").strip()
    if not raw:
        raise AuthError("missing_token")
    secret = _jwt_secret()
    issuer = _issuer()
    if not secret and not issuer:
        raise AuthConfigError("SUPABASE_JWT_SECRET or SUPABASE_URL is required")
    try:
        header = jwt.get_unverified_header(raw)
    except jwt.PyJWTError as e:
        raise AuthError("invalid_token") from e
    alg = str(header.get("alg") or "")
    options = {"require": ["exp", "sub"]}
    decode_kw: dict = {
        "audience": "authenticated",
        "leeway": 30,
        "options": options,
    }
    if issuer:
        decode_kw["issuer"] = issuer
    try:
        if alg == "HS256":
            if not secret:
                raise AuthConfigError("SUPABASE_JWT_SECRET is required for HS256 tokens")
            claims = jwt.decode(raw, secret, algorithms=["HS256"], **decode_kw)
        elif alg in ("ES256", "RS256"):
            key = _jwks().get_signing_key_from_jwt(raw).key
            claims = jwt.decode(raw, key, algorithms=[alg], **decode_kw)
        else:
            raise AuthError("invalid_token")
    except AuthConfigError:
        raise
    except jwt.PyJWTError as e:
        raise AuthError("invalid_token") from e
    sub = str(claims.get("sub") or "").strip()
    try:
        uuid.UUID(sub)
    except ValueError as e:
        raise AuthError("invalid_token") from e
    return claims


# Public mailbox providers. Matching on these would put strangers in one org.
_PUBLIC_EMAIL_DOMAINS = frozenset({
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "live.com",
    "msn.com",
    "yahoo.com",
    "ymail.com",
    "icloud.com",
    "me.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "gmx.com",
})


def _workspace_name(email: str | None) -> str:
    raw = (email or "").strip()
    local = raw.split("@", 1)[0].strip() if raw else ""
    if local:
        return f"{local}'s workspace"[:80]
    return "Workspace"


def _signup_domain(email: str | None) -> str | None:
    """Company domain for auto-join, or None if missing/malformed."""
    raw = (email or "").strip()
    if not raw or "@" not in raw:
        return None
    local, domain = raw.rsplit("@", 1)
    local = local.strip()
    domain = domain.lower().strip()
    if not local or not domain:
        return None
    return domain


def _optional_name(value: object) -> str | None:
    """JWT / form names: strip, empty → None. Cap length. Not refreshed on login."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:80]


def ensure_membership(
    user_id: str,
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> Membership:
    """One membership per user at launch.

    New signups join by email domain, or get a personal org for public
    providers (gmail, outlook, …). Human signup never claims DEFAULT_ORG_ID.
    first_name/last_name are written only on the new-member INSERT, not on
    later logins (MVP: names are captured once).
    """
    uid = str(uuid.UUID(str(user_id)))
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, user_id=uid)
        row = conn.execute(
            """
            SELECT org_id, role FROM org_members
            WHERE user_id = %s
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        if row:
            org_id = str(row["org_id"])
            _ensure_placeholder_rubric(conn, org_id=org_id, user_id=uid)
            return Membership(org_id, str(row["role"]), uid)

        conn.execute("LOCK TABLE org_members IN EXCLUSIVE MODE")
        row = conn.execute(
            """
            SELECT org_id, role FROM org_members
            WHERE user_id = %s
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        if row:
            org_id = str(row["org_id"])
            _ensure_placeholder_rubric(conn, org_id=org_id, user_id=uid)
            return Membership(org_id, str(row["role"]), uid)

        domain = _signup_domain(email)
        created = False
        if domain is None or domain in _PUBLIC_EMAIL_DOMAINS:
            org_id = str(uuid.uuid4())
            db.apply_tenant_gucs(conn, org_id=org_id, user_id=uid)
            conn.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s)",
                (org_id, _workspace_name(email)),
            )
            created = True
        else:
            org_id = str(uuid.uuid4())
            db.apply_tenant_gucs(conn, org_id=org_id, user_id=uid)
            inserted = conn.execute(
                """
                INSERT INTO orgs (id, name, domain)
                VALUES (%s, %s, %s)
                ON CONFLICT (domain) DO NOTHING
                RETURNING id
                """,
                (org_id, domain, domain),
            ).fetchone()
            if inserted:
                org_id = str(inserted["id"])
                created = True
            else:
                found = conn.execute(
                    "SELECT public.org_id_for_domain(%s) AS id",
                    (domain,),
                ).fetchone()
                if not found or not found["id"]:
                    raise RuntimeError("domain conflict without an existing org")
                org_id = str(found["id"])
                db.apply_tenant_gucs(conn, org_id=org_id, user_id=uid)

        role = "owner" if created else "member"
        if created:
            audit_store.seed_legacy_rubric(
                conn, org_id=org_id, rubric_id=str(uuid.uuid4()),
            )

        db.apply_tenant_gucs(conn, org_id=org_id, user_id=uid)
        _ensure_placeholder_rubric(conn, org_id=org_id, user_id=uid)
        try:
            conn.execute(
                """
                INSERT INTO org_members (
                    org_id, user_id, role, first_name, last_name
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    org_id,
                    uid,
                    role,
                    _optional_name(first_name),
                    _optional_name(last_name),
                ),
            )
        except UniqueViolation:
            conn.rollback()
            db.apply_tenant_gucs(conn, user_id=uid)
            row = conn.execute(
                """
                SELECT org_id, role FROM org_members
                WHERE user_id = %s
                LIMIT 1
                """,
                (uid,),
            ).fetchone()
            if not row:
                raise
            _ensure_placeholder_rubric(
                conn, org_id=str(row["org_id"]), user_id=uid,
            )
            return Membership(str(row["org_id"]), str(row["role"]), uid)

        return Membership(org_id, role, uid)


def ensure_placeholder_org(conn) -> None:
    """Idempotent seed of the DEFAULT_ORG_ID orgs row only.

    Background/webhook/CLI fallbacks insert into calls/usage with that org_id.
    Migration 0001 seeds it once; a data wipe can remove it. Same idea as
    _ensure_placeholder_rubric. Not a general org factory — do not pass
    another id. Do not call from JWT signup/login bootstrap.

    RLS on orgs is id = current_org_id(); GUC is set to the placeholder
    for this insert. Caller must already be on the default-org fallback
    path (or re-apply tenant GUCs afterward).
    """
    db.apply_tenant_gucs(conn, org_id=DEFAULT_ORG_ID)
    conn.execute(
        """
        INSERT INTO orgs (id, name) VALUES (%s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (DEFAULT_ORG_ID, "default"),
    )


def _ensure_placeholder_rubric(conn, *, org_id: str, user_id: str) -> None:
    """Re-seed Default (legacy v8) if a data wipe removed it. No-op for other orgs."""
    if org_id != DEFAULT_ORG_ID:
        return
    db.apply_tenant_gucs(conn, org_id=org_id, user_id=user_id)
    audit_store.seed_legacy_rubric(
        conn, org_id=org_id, rubric_id=DEFAULT_RUBRIC_ID,
    )


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return None


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT or path in _PUBLIC_PATHS:
        return True
    return False


def _auth_failure(request: Request, status: int, detail: str) -> JSONResponse:
    headers = {}
    origin = request.headers.get("origin") or ""
    if origin in cors_origins():
        headers["access-control-allow-origin"] = origin
        headers["vary"] = "Origin"
    return JSONResponse({"detail": detail}, status_code=status, headers=headers)


class JwtAuthMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)
        if request.method == "OPTIONS" or _is_public(request.url.path):
            await self.app(scope, receive, send)
            return
        if not auth_configured():
            response = _auth_failure(request, 503, "Auth is not configured")
            await response(scope, receive, send)
            return
        token = _bearer(request)
        if not token:
            response = _auth_failure(request, 401, "Not authenticated")
            await response(scope, receive, send)
            return
        try:
            claims = verify_access_token(token)
            sub = str(claims["sub"])
            email = claims.get("email")
            email_s = email.strip() if isinstance(email, str) else None
            raw_meta = claims.get("user_metadata") or {}
            user_meta = raw_meta if isinstance(raw_meta, dict) else {}
            first_name = _optional_name(user_meta.get("first_name"))
            last_name = _optional_name(user_meta.get("last_name"))
        except AuthConfigError:
            response = _auth_failure(request, 503, "Auth is not configured")
            await response(scope, receive, send)
            return
        except AuthError:
            response = _auth_failure(request, 401, "Invalid or expired token")
            await response(scope, receive, send)
            return
        user_token = bind_user_id(sub)
        org_token = None
        try:
            membership = ensure_membership(sub, email_s, first_name, last_name)
            request.state.user_id = membership.user_id
            request.state.org_id = membership.org_id
            request.state.role = membership.role
            request.state.email = email_s
            org_token = bind_org_id(membership.org_id)
            await self.app(scope, receive, send)
        finally:
            if org_token is not None:
                reset_org_id(org_token)
            reset_user_id(user_token)


def org_id_from_request(request: Request) -> str:
    """Tenant id from the verified JWT membership only.

    Never reads query params, path params, headers (other than the already-
    verified Bearer token), or the request body.
    """
    org = parse_org_id(getattr(request.state, "org_id", None))
    if not org:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return org


def is_platform_admin(request: Request) -> bool:
    """True iff the verified JWT email is on PLATFORM_ADMIN_EMAILS. Fail closed."""
    allowed = {
        e.strip().lower()
        for e in (os.getenv("PLATFORM_ADMIN_EMAILS") or "").split(",")
        if e.strip()
    }
    email = (getattr(request.state, "email", None) or "").strip().lower()
    return bool(email and email in allowed)


def require_platform_admin(request: Request) -> None:
    """Allowlist of JWT emails. Empty PLATFORM_ADMIN_EMAILS denies everyone."""
    if not is_platform_admin(request):
        raise HTTPException(status_code=403, detail="Not authorized.")


def require_owner(request: Request) -> None:
    """Self-serve rubric builder: only the org's account owner may edit it.

    org_members.role is only "owner" or "member" today — no team-admin tier
    yet (see the roles hierarchy doc). Owner-only until that ships.
    """
    role = (getattr(request.state, "role", None) or "").strip().lower()
    if role != "owner":
        raise HTTPException(status_code=403, detail="Only the account owner can do this.")
