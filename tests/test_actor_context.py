"""AC-64: actor identity on every request — the foundation the rest of the
Actor Identity & Audit Trail epic depends on.

bound_user_id() (org_ids.py) already existed and was already bound in
auth.py's JwtAuthMiddleware, but nothing ever read it back into a log
line. This adds actor_email/actor_ip alongside it (same contextvar
pattern) and wires api.py's log_http middleware to attach all three to
every http_request/http_error event."""

from __future__ import annotations

import logging
import uuid

from backend import org_ids


def test_actor_email_round_trips_through_bind_and_reset():
    assert org_ids.bound_actor_email() is None
    token = org_ids.bind_actor_email("agent@example.com")
    try:
        assert org_ids.bound_actor_email() == "agent@example.com"
    finally:
        org_ids.reset_actor_email(token)
    assert org_ids.bound_actor_email() is None


def test_actor_email_strips_whitespace_and_treats_empty_as_none():
    token = org_ids.bind_actor_email("  agent@example.com  ")
    try:
        assert org_ids.bound_actor_email() == "agent@example.com"
    finally:
        org_ids.reset_actor_email(token)

    token = org_ids.bind_actor_email("")
    try:
        assert org_ids.bound_actor_email() is None
    finally:
        org_ids.reset_actor_email(token)

    token = org_ids.bind_actor_email(None)
    try:
        assert org_ids.bound_actor_email() is None
    finally:
        org_ids.reset_actor_email(token)


def test_actor_ip_round_trips_through_bind_and_reset():
    assert org_ids.bound_actor_ip() is None
    token = org_ids.bind_actor_ip("203.0.113.5")
    try:
        assert org_ids.bound_actor_ip() == "203.0.113.5"
    finally:
        org_ids.reset_actor_ip(token)
    assert org_ids.bound_actor_ip() is None


def test_actor_ip_treats_empty_and_none_as_none():
    for raw in ("", None):
        token = org_ids.bind_actor_ip(raw)
        try:
            assert org_ids.bound_actor_ip() is None
        finally:
            org_ids.reset_actor_ip(token)


def test_jwt_auth_middleware_binds_actor_email_and_ip_for_the_requests_lifetime(monkeypatch):
    """Unit-tests the middleware directly against a minimal ASGI inner app —
    same style as applog's own test_request_id_middleware_binds_a_fresh_id_per_request
    — rather than mutating the shared api.app singleton."""
    import asyncio

    from backend import auth
    from backend.auth import Membership

    uid = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            org_id, "owner", str(user_id),
        ),
    )
    monkeypatch.setattr("backend.auth.auth_configured", lambda: True)
    monkeypatch.setattr(
        "backend.auth.verify_access_token",
        lambda token: {"sub": uid, "email": "someone@example.com"},
    )
    monkeypatch.setattr("backend.auth._bearer", lambda request: "fake-token")

    seen: list[tuple[str | None, str | None]] = []

    async def inner_app(scope, receive, send):
        seen.append((org_ids.bound_actor_email(), org_ids.bound_actor_ip()))

    middleware = auth.JwtAuthMiddleware(inner_app)

    async def _one_request():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/whatever",
            "headers": [],
            "client": ("198.51.100.7", 12345),
        }
        await middleware(scope, None, None)

    asyncio.run(_one_request())

    assert seen == [("someone@example.com", "198.51.100.7")]
    # bound only for the request's lifetime — nothing leaks after it ends
    assert org_ids.bound_actor_email() is None
    assert org_ids.bound_actor_ip() is None


def test_log_http_attaches_actor_fields_to_the_http_request_event(monkeypatch):
    """End-to-end through the real TestClient + real auth middleware stack:
    an authenticated request's http_request log line must carry actor_id/
    actor_email/ip_address, with zero per-endpoint code changes."""
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    captured: list[dict] = []
    real_event = __import__("backend.applog", fromlist=["event"]).event

    def _capture(logger, name, level=logging.INFO, **fields):
        if name == "http_request":
            captured.append(fields)
        return real_event(logger, name, level, **fields)

    monkeypatch.setattr("backend.api.applog.event", _capture)

    client = TestClient(app)
    uid = authorize(client, monkeypatch)
    r = client.get("/api/tickets")
    assert r.status_code == 200

    matching = [f for f in captured if f.get("path") == "/api/tickets"]
    assert matching, "expected an http_request event for GET /api/tickets"
    fields = matching[-1]
    assert fields["actor_id"] == uid
    assert fields["actor_email"] == "tester@example.com"  # conftest's mint_access_token default
    assert fields["ip_address"]  # TestClient's synthetic client host, non-empty


def test_log_http_omits_actor_fields_as_none_for_a_public_unauthenticated_path(monkeypatch):
    """/api/integrations/justcall/webhook is on auth.py's public-path
    allowlist — JwtAuthMiddleware never runs its bind block for it, so
    log_http must still fire (unlike the 401 case, where _auth_failure()
    short-circuits before log_http is ever reached) with actor fields
    genuinely absent rather than erroring."""
    from fastapi.testclient import TestClient

    from backend.api import app

    captured: list[dict] = []
    real_event = __import__("backend.applog", fromlist=["event"]).event

    def _capture(logger, name, level=logging.INFO, **fields):
        if name == "http_error":
            captured.append(fields)
        return real_event(logger, name, level, **fields)

    monkeypatch.setattr("backend.api.applog.event", _capture)

    client = TestClient(app)
    # GET on a POST-only public webhook route — 405, routed without ever
    # reaching JwtAuthMiddleware's bind block (the path is public).
    r = client.get("/api/integrations/justcall/webhook")
    assert r.status_code == 405

    matching = [f for f in captured if f.get("path") == "/api/integrations/justcall/webhook"]
    assert matching
    fields = matching[-1]
    assert fields["actor_id"] is None
    assert fields["actor_email"] is None
