"""CL-8: Supabase JWT verification, membership bootstrap, 401 on data routes."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.auth import AuthError, ensure_membership, verify_access_token
from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize, mint_access_token


class _Row:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, *, existing: list[dict] | None = None, orgs: list | None = None):
        self.existing = list(existing or [])
        self.inserted_orgs: list = list(orgs or [])
        self.inserted_members: list = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "LOCK TABLE" in norm:
            return _Row(None)
        if "SELECT ORG_ID, ROLE FROM ORG_MEMBERS" in norm:
            uid = params[0] if params else None
            for row in self.existing:
                if str(row["user_id"]) == str(uid):
                    return _Row({"org_id": row["org_id"], "role": row["role"]})
            return _Row(None)
        if "ORG_ID_FOR_DOMAIN" in norm:
            domain = params[0] if params else None
            for org in self.inserted_orgs:
                if len(org) > 2 and org[2] == domain:
                    return _Row({"id": org[0]})
            return _Row(None)
        if "INSERT INTO ORGS" in norm:
            if "ON CONFLICT" in norm:
                domain = params[2] if params and len(params) > 2 else None
                for org in self.inserted_orgs:
                    if len(org) > 2 and org[2] == domain:
                        return _Row(None)
                self.inserted_orgs.append(params)
                return _Row({"id": params[0]})
            self.inserted_orgs.append(params)
            return _Row(None)
        if "INSERT INTO ORG_MEMBERS" in norm:
            role = params[2] if params and len(params) > 2 else "owner"
            self.inserted_members.append(params)
            self.existing.append(
                {"org_id": params[0], "user_id": params[1], "role": role}
            )
            return _Row(None)
        return _Row(None)


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.db.connection", _cm)
    monkeypatch.setattr("backend.auth.audit_store.seed_legacy_rubric", lambda *a, **k: None)
    yield conn


def test_verify_access_token_accepts_hs256():
    sub = str(uuid.uuid4())
    token = mint_access_token(sub=sub)
    claims = verify_access_token(token)
    assert claims["sub"] == sub


def test_verify_access_token_rejects_expired():
    token = mint_access_token(exp_delta=-120)
    with pytest.raises(AuthError):
        verify_access_token(token)


def test_verify_access_token_rejects_wrong_secret():
    token = mint_access_token(secret="other-test-only-secret-not-for-prod-use")
    with pytest.raises(AuthError):
        verify_access_token(token)


def test_verify_access_token_rejects_wrong_audience():
    token = mint_access_token(aud="anon")
    with pytest.raises(AuthError):
        verify_access_token(token)


def test_health_is_public():
    from backend.api import app

    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_data_routes_401_without_token():
    from backend.api import app

    client = TestClient(app)
    for path in ("/api/calls", "/api/me", "/api/pyai/status"):
        r = client.get(path)
        assert r.status_code == 401, path


def test_data_routes_401_with_garbage_token():
    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/calls", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_me_returns_membership_from_jwt(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    uid = authorize(client, monkeypatch)
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == uid
    assert body["org_id"] == DEFAULT_ORG_ID
    assert body["role"] == "owner"


def test_justcall_webhook_stays_public():
    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/integrations/justcall/webhook", json={"type": "url.validation"})
    assert r.status_code == 200


def test_ensure_membership_domain_match(monkeypatch):
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        owner_id = str(uuid.uuid4())
        member_id = str(uuid.uuid4())
        first = ensure_membership(owner_id, "ada@Acme.com")
        second = ensure_membership(member_id, "lin@acme.com")

    assert first.org_id == second.org_id
    assert first.org_id != DEFAULT_ORG_ID
    assert first.role == "owner"
    assert second.role == "member"
    assert first.user_id == owner_id
    assert second.user_id == member_id
    assert first.user_id != second.user_id
    assert conn.inserted_orgs[0][1] == "acme.com"
    assert conn.inserted_orgs[0][2] == "acme.com"
    assert len(conn.inserted_orgs) == 1
    assert conn.inserted_members[0][2] == "owner"
    assert conn.inserted_members[1][2] == "member"


def test_ensure_membership_blocklist_bypass(monkeypatch):
    conn = _FakeConn(
        existing=[{"org_id": DEFAULT_ORG_ID, "user_id": str(uuid.uuid4()), "role": "owner"}]
    )
    with _fake_db(monkeypatch, conn):
        a = ensure_membership(str(uuid.uuid4()), "ada@gmail.com")
        b = ensure_membership(str(uuid.uuid4()), "lin@outlook.com")
        c = ensure_membership(str(uuid.uuid4()), None)
        d = ensure_membership(str(uuid.uuid4()), "not-an-email")

    ids = {a.org_id, b.org_id, c.org_id, d.org_id}
    assert len(ids) == 4
    assert DEFAULT_ORG_ID not in ids
    assert a.role == b.role == c.role == d.role == "owner"
    assert all(len(org) == 2 for org in conn.inserted_orgs)
    assert conn.inserted_orgs[0][1] == "ada's workspace"
    assert conn.inserted_orgs[1][1] == "lin's workspace"


def test_ensure_membership_same_domain_race(monkeypatch):
    existing_org = str(uuid.uuid4())
    conn = _FakeConn(orgs=[(existing_org, "acme.com", "acme.com")])
    with _fake_db(monkeypatch, conn):
        uid = str(uuid.uuid4())
        m = ensure_membership(uid, "sam@acme.com")

    assert m.org_id == existing_org
    assert m.role == "member"
    assert m.user_id == uid
    assert len(conn.inserted_orgs) == 1
    assert conn.inserted_members == [(existing_org, uid, "member")]


def test_ensure_membership_later_user_new_domain_gets_new_org(monkeypatch):
    other = str(uuid.uuid4())
    conn = _FakeConn(
        existing=[{"org_id": DEFAULT_ORG_ID, "user_id": other, "role": "owner"}]
    )
    with _fake_db(monkeypatch, conn):
        uid = str(uuid.uuid4())
        m = ensure_membership(uid, "lin@example.com")

    assert m.org_id != DEFAULT_ORG_ID
    assert m.role == "owner"
    assert conn.inserted_orgs[0][1] == "example.com"
    assert conn.inserted_orgs[0][2] == "example.com"
    assert conn.inserted_members[0][1] == uid
