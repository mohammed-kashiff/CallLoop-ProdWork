"""Supabase JWT verification and org membership bootstrap (CL-8).

Auth only: callers get request.state.user_id / org_id / role. Handlers still
use DEFAULT_ORG_ID until CL-9. Never log the token. Never take org_id from
the client.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse
from jwt import PyJWKClient
from psycopg.errors import UniqueViolation
from starlette.types import ASGIApp, Receive, Scope, Send

from . import audit_store
from . import db
from .config import cors_origins
from .org_ids import DEFAULT_ORG_ID

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


def _workspace_name(email: str | None) -> str:
    raw = (email or "").strip()
    local = raw.split("@", 1)[0].strip() if raw else ""
    if local:
        return f"{local}'s workspace"[:80]
    return "Workspace"


def ensure_membership(user_id: str, email: str | None = None) -> Membership:
    """One membership per user at launch. First user claims DEFAULT_ORG_ID."""
    uid = str(uuid.UUID(str(user_id)))
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT org_id, role FROM org_members
            WHERE user_id = %s
            LIMIT 1
            """,
            (uid,),
        ).fetchone()
        if row:
            return Membership(str(row["org_id"]), str(row["role"]), uid)

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
            return Membership(str(row["org_id"]), str(row["role"]), uid)

        existing = conn.execute("SELECT 1 FROM org_members LIMIT 1").fetchone()
        if existing is None:
            org_id = DEFAULT_ORG_ID
        else:
            org_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s)",
                (org_id, _workspace_name(email)),
            )
            audit_store.seed_legacy_rubric(
                conn, org_id=org_id, rubric_id=str(uuid.uuid4()),
            )

        try:
            conn.execute(
                """
                INSERT INTO org_members (org_id, user_id, role)
                VALUES (%s, %s, 'owner')
                """,
                (org_id, uid),
            )
        except UniqueViolation:
            conn.rollback()
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
            return Membership(str(row["org_id"]), str(row["role"]), uid)

        return Membership(org_id, "owner", uid)


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
            membership = ensure_membership(sub, email_s)
        except AuthConfigError:
            response = _auth_failure(request, 503, "Auth is not configured")
            await response(scope, receive, send)
            return
        except AuthError:
            response = _auth_failure(request, 401, "Invalid or expired token")
            await response(scope, receive, send)
            return
        request.state.user_id = membership.user_id
        request.state.org_id = membership.org_id
        request.state.role = membership.role
        await self.app(scope, receive, send)
