"""Owner invite: no password, JWT org only, member 403."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token
from tests.test_team_performance import AGENT_A, AGENT_B

SRC = (ROOT / "backend" / "org_invite.py").read_text(encoding="utf-8")
TEST_SERVICE_ROLE = "test-only-service-role-not-a-production-secret"


class _Row:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, *, membership_org=None):
        self.membership_org = membership_org
        self.inserted = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "FROM ORG_MEMBERS" in norm and "INSERT" not in norm:
            if self.membership_org:
                return _Row({"org_id": self.membership_org})
            return _Row(None)
        if "INSERT INTO ORG_MEMBERS" in norm:
            self.inserted.append(params)
            return _Row(None)
        return _Row(None)


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.org_invite.db.connection", _cm)
    yield conn


def _auth_http(monkeypatch, *, user_id: str | None = None, existing=None):
    uid = user_id or str(uuid.uuid4())
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", TEST_SERVICE_ROLE)

    def _get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if existing:
            resp.json.return_value = {"users": [{"id": existing, "email": kwargs.get("params", {}).get("email")}]}
        else:
            resp.json.return_value = {"users": []}
        return resp

    def _post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        body = {"id": uid, "email": kwargs.get("json", {}).get("email")}
        resp.json.return_value = body
        _post.calls.append({"url": url, "json": kwargs.get("json")})
        return resp

    _post.calls = []
    monkeypatch.setattr("backend.org_invite.httpx.get", _get)
    monkeypatch.setattr("backend.org_invite.httpx.post", _post)
    monkeypatch.setattr("backend.org_invite.httpx.delete", MagicMock())
    return uid, _post


def test_invite_never_mentions_password_or_invite_url():
    lowered = SRC.lower()
    assert "temporary_password" not in lowered
    assert "token_urlsafe" not in SRC
    assert "action_link" not in lowered
    for line in SRC.splitlines():
        stripped = line.strip()
        if stripped.startswith("log.") or "applog.event" in stripped:
            assert "password" not in stripped.lower()
            assert "email" not in stripped.lower()
            assert "redirect_to" not in stripped.lower()


def test_invite_uses_customer_origin_reset_password():
    assert "CUSTOMER_ORIGIN" in SRC
    assert "/reset-password" in SRC
    assert "/auth/v1/invite" in SRC
    assert "require_owner" in SRC
    assert "bypass_rls" not in SRC
    assert "%s" in SRC


def test_invite_inserts_member_without_password(monkeypatch):
    from backend.org_invite import invite_member

    uid, posted = _auth_http(monkeypatch)
    conn = _FakeConn()
    monkeypatch.setattr("backend.org_invite.audit_log.record", lambda *a, **k: None)
    with _fake_db(monkeypatch, conn):
        out = invite_member(
            org_id=DEFAULT_ORG_ID,
            email="Pat.New@gmail.com",
            first_name="Pat",
            last_name="New",
        )
    assert out["user_id"] == uid
    assert out["email"] == "pat.new@gmail.com"
    assert out["role"] == "member"
    assert "temporary_password" not in out
    assert "password" not in out
    assert len(conn.inserted) == 1
    assert conn.inserted[0][2] == "member"
    assert posted.calls[0]["url"].endswith("/auth/v1/invite")
    assert "password" not in posted.calls[0]["json"]
    assert posted.calls[0]["json"]["redirect_to"].endswith("/reset-password")


def test_invite_existing_other_org_is_409(monkeypatch):
    from backend.org_invite import invite_member
    from fastapi import HTTPException

    other = str(uuid.uuid4())
    existing = str(uuid.uuid4())
    _auth_http(monkeypatch, existing=existing)
    conn = _FakeConn(membership_org=other)
    with _fake_db(monkeypatch, conn):
        try:
            invite_member(
                org_id=DEFAULT_ORG_ID,
                email="taken@gmail.com",
                first_name="Pat",
                last_name="New",
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            return
    raise AssertionError("expected 409")


def test_http_member_cannot_invite(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    member = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    member.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_B)}"
    r = member.post(
        "/api/team/invite",
        json={"email": "new@gmail.com", "first_name": "Pat", "last_name": "New"},
    )
    assert r.status_code == 403


def test_http_owner_invite_ok(monkeypatch):
    from backend.api import app

    monkeypatch.setattr(
        "backend.org_invite.invite_member",
        lambda **k: {"email": k["email"].strip().lower(), "user_id": str(uuid.uuid4()), "role": "member"},
    )
    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_A, org_id=DEFAULT_ORG_ID)
    r = client.post(
        "/api/team/invite",
        json={"email": "new@gmail.com", "first_name": "Pat", "last_name": "New"},
    )
    assert r.status_code == 200
    body = r.json()
    assert "temporary_password" not in body
    assert body["role"] == "member"
