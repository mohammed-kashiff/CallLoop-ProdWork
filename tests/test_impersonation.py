"""CC-1: platform-admin "log in as" a customer. Admin-only, no live consent
step — impersonation_log is the accountability record, not a gate.

Mirrors test_admin_provision.py's httpx-mocking style: the Supabase Admin
REST API is called directly via httpx, not the supabase-py SDK."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize, mint_access_token

TEST_SERVICE_ROLE = "test-only-service-role-not-a-production-secret"
TARGET_USER = str(uuid.uuid4())
TARGET_ROW = {
    "user_id": TARGET_USER,
    "email": "customer@acme.com",
    "org_id": DEFAULT_ORG_ID,
    "org_name": "Acme",
    "role": "owner",
}


class _InsertConn:
    def __init__(self):
        self.inserts: list = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "INSERT INTO IMPERSONATION_LOG" in norm:
            self.inserts.append(params)
        return self


@contextmanager
def _fake_db(monkeypatch, conn: _InsertConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.impersonation.db.connection", _cm)
    monkeypatch.setattr("backend.impersonation.org_scope", lambda oid: _null_ctx())
    yield conn


@contextmanager
def _null_ctx():
    yield


def _mock_supabase(monkeypatch, *, generate_payload=None, verify_payload=None, fail_at=None):
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", TEST_SERVICE_ROLE)
    calls = []

    def _post(url, **kwargs):
        calls.append({"url": url, "json": kwargs.get("json"), "headers": kwargs.get("headers")})
        resp = MagicMock()
        if "/admin/generate_link" in url:
            if fail_at == "generate_link":
                raise __import__("httpx").ConnectError("boom")
            resp.status_code = 200
            resp.json.return_value = generate_payload if generate_payload is not None else {
                "properties": {"hashed_token": "hashed-abc"},
            }
            resp.raise_for_status = lambda: None
            return resp
        if "/verify" in url:
            if fail_at == "verify":
                raise __import__("httpx").ConnectError("boom")
            resp.status_code = 200
            resp.json.return_value = verify_payload if verify_payload is not None else {
                "access_token": "at-123", "refresh_token": "rt-123",
                "expires_in": 3600, "token_type": "bearer",
            }
            resp.raise_for_status = lambda: None
            return resp
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr("backend.impersonation.httpx.post", _post)
    return calls


def _mock_directory(monkeypatch, row=TARGET_ROW):
    monkeypatch.setattr(
        "backend.impersonation.search_directory",
        lambda q: {"rows": [row] if row else []},
    )


# ---------- start_impersonation() ----------


def test_happy_path_calls_generate_link_then_verify_and_records_audit(monkeypatch):
    from backend import impersonation

    _mock_directory(monkeypatch)
    calls = _mock_supabase(monkeypatch)
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        out = impersonation.start_impersonation(
            TARGET_USER, admin_email="admin@calloop.com", ip_address="1.2.3.4",
        )

    assert out["access_token"] == "at-123"
    assert out["refresh_token"] == "rt-123"
    assert out["org_id"] == DEFAULT_ORG_ID
    assert out["org_name"] == "Acme"
    assert out["target_email"] == "customer@acme.com"

    assert len(calls) == 2
    assert calls[0]["json"] == {"type": "magiclink", "email": "customer@acme.com"}
    assert calls[1]["json"] == {"type": "magiclink", "token_hash": "hashed-abc"}
    assert calls[1]["headers"]["apikey"] == TEST_SERVICE_ROLE

    assert len(conn.inserts) == 1
    org_id, admin_email, target_user_id, target_email, ip = conn.inserts[0]
    assert org_id == DEFAULT_ORG_ID
    assert admin_email == "admin@calloop.com"
    assert target_user_id == TARGET_USER
    assert target_email == "customer@acme.com"
    assert ip == "1.2.3.4"


def test_hashed_token_found_at_top_level_too(monkeypatch):
    """Confirmed against the real project (2026-09-04): hashed_token comes
    back top-level, not under "properties". Keep accepting both shapes
    anyway — Supabase's public docs don't pin this down precisely, and a
    "properties"-nested response has also been documented elsewhere."""
    from backend import impersonation

    _mock_directory(monkeypatch)
    _mock_supabase(monkeypatch, generate_payload={"hashed_token": "top-level-hash"})
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        out = impersonation.start_impersonation(TARGET_USER, admin_email="a@x.com")
    assert out["access_token"] == "at-123"


def test_unknown_user_id_404s_before_any_supabase_call(monkeypatch):
    from backend import impersonation

    _mock_directory(monkeypatch, row=None)
    calls = _mock_supabase(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        impersonation.start_impersonation(TARGET_USER, admin_email="a@x.com")
    assert exc.value.status_code == 404
    assert calls == []


def test_generate_link_failure_502s_before_verify_and_before_audit_row(monkeypatch):
    from backend import impersonation

    _mock_directory(monkeypatch)
    _mock_supabase(monkeypatch, fail_at="generate_link")
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            impersonation.start_impersonation(TARGET_USER, admin_email="a@x.com")
    assert exc.value.status_code == 502
    assert conn.inserts == []


def test_verify_failure_502s_and_never_writes_an_audit_row(monkeypatch):
    """A half-succeeded mint (link issued, session exchange failed) must
    never look like a completed impersonation in the audit trail."""
    from backend import impersonation

    _mock_directory(monkeypatch)
    _mock_supabase(monkeypatch, fail_at="verify")
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            impersonation.start_impersonation(TARGET_USER, admin_email="a@x.com")
    assert exc.value.status_code == 502
    assert conn.inserts == []


def test_missing_access_token_in_verify_response_502s(monkeypatch):
    from backend import impersonation

    _mock_directory(monkeypatch)
    _mock_supabase(monkeypatch, verify_payload={"msg": "confirmation required"})
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            impersonation.start_impersonation(TARGET_USER, admin_email="a@x.com")
    assert exc.value.status_code == 502
    assert conn.inserts == []


# ---------- POST /api/admin/users/{user_id}/impersonate ----------


def test_non_admin_gets_403_and_never_calls_supabase(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    _mock_directory(monkeypatch)
    calls = _mock_supabase(monkeypatch)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(f"/api/admin/users/{TARGET_USER}/impersonate")
    assert r.status_code == 403
    assert calls == []


def test_invalid_user_id_400s(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/admin/users/not-a-uuid/impersonate")
    assert r.status_code == 400


def test_admin_can_impersonate_over_http_and_admin_email_is_recorded(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    _mock_directory(monkeypatch)
    _mock_supabase(monkeypatch)
    conn = _InsertConn()
    with _fake_db(monkeypatch, conn):
        from backend.api import app

        client = TestClient(app)
        authorize(client, monkeypatch)
        client.headers["Authorization"] = (
            f"Bearer {mint_access_token(email='tester@example.com')}"
        )
        r = client.post(f"/api/admin/users/{TARGET_USER}/impersonate")
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "at-123"
    assert body["target_email"] == "customer@acme.com"
    assert len(conn.inserts) == 1
    assert conn.inserts[0][1] == "tester@example.com"
