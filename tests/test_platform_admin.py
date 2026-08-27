"""Platform admin allowlist: fail closed, JWT email only."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from backend.auth import JwtAuthMiddleware, require_platform_admin
from tests.conftest import authorize, mint_access_token


def _admin_app():
    app = FastAPI()
    app.add_middleware(JwtAuthMiddleware)

    @app.get("/api/admin/ping")
    def ping(request: Request):
        require_platform_admin(request)
        return {"ok": True, "email": getattr(request.state, "email", None)}

    return app


def test_empty_platform_admin_emails_denies(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    client = TestClient(_admin_app())
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ping")
    assert r.status_code == 403
    assert r.json() == {"detail": "Not authorized."}


def test_blank_platform_admin_emails_denies(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "  ,  ")
    client = TestClient(_admin_app())
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ping")
    assert r.status_code == 403


def test_jwt_email_not_on_allowlist_gets_403(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "admin@example.com")
    client = TestClient(_admin_app())
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ping")
    assert r.status_code == 403
    assert r.json() == {"detail": "Not authorized."}


def test_allowlisted_email_is_permitted(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "Admin@Example.com, other@x.com")
    from backend.auth import Membership
    from backend.org_ids import DEFAULT_ORG_ID

    client = TestClient(_admin_app())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "owner", str(user_id)
        ),
    )
    client.headers["Authorization"] = (
        f"Bearer {mint_access_token(email='admin@example.com')}"
    )
    r = client.get("/api/admin/ping")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["email"] == "admin@example.com"


def test_require_platform_admin_empty_env_on_bare_request(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    request = StarletteRequest(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/admin/ping",
            "raw_path": b"/api/admin/ping",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("test", 80),
        }
    )
    request.state.email = "anyone@example.com"
    with pytest.raises(HTTPException) as exc:
        require_platform_admin(request)
    assert exc.value.status_code == 403
