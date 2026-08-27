"""AC-3: platform-admin provisioning. Password is never logged."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.admin_provision import provision_user
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token

TEST_SERVICE_ROLE = "test-only-service-role-not-a-production-secret"
EXISTING_ORG = "00000000-0000-4000-8000-0000000000aa"


class _Row:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    def __init__(self, *, orgs: list[dict] | None = None):
        self.orgs = list(orgs or [])
        self.inserted_orgs: list = []
        self.inserted_members: list = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "SELECT ID, NAME FROM ORGS" in norm:
            oid = params[0] if params else None
            for org in self.orgs:
                if str(org["id"]) == str(oid):
                    return _Row({"id": org["id"], "name": org["name"]})
            return _Row(None)
        if "INSERT INTO ORGS" in norm:
            self.inserted_orgs.append(params)
            self.orgs.append({"id": params[0], "name": params[1]})
            return _Row(None)
        if "INSERT INTO ORG_MEMBERS" in norm:
            self.inserted_members.append(params)
            return _Row(None)
        return _Row(None)


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.admin_provision.db.connection", _cm)
    monkeypatch.setattr(
        "backend.admin_provision.audit_store.seed_legacy_rubric",
        lambda *a, **k: None,
    )
    yield conn


def _auth_ok(monkeypatch, *, user_id: str | None = None):
    uid = user_id or str(uuid.uuid4())
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", TEST_SERVICE_ROLE)

    def _post(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": uid, "email": kwargs.get("json", {}).get("email")}
        _post.calls.append({"url": url, "json": kwargs.get("json")})
        return resp

    _post.calls = []
    monkeypatch.setattr("backend.admin_provision.httpx.post", _post)
    monkeypatch.setattr("backend.admin_provision.httpx.delete", MagicMock())
    return uid, _post


def test_provision_module_never_logs_password():
    src = (ROOT / "backend" / "admin_provision.py").read_text(encoding="utf-8")
    assert "import random" not in src
    assert "random." not in src
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("log.") or "applog.event" in stripped:
            lowered = stripped.lower()
            assert "temporary_password" not in lowered
            assert "password" not in lowered
            assert "token_urlsafe" not in lowered


def test_new_org_creates_one_org_and_owner(monkeypatch):
    uid, posted = _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="Pat.New@gmail.com",
            first_name="Pat",
            last_name="New",
            org_mode="new",
        )
    assert len(conn.inserted_orgs) == 1
    assert len(conn.inserted_members) == 1
    assert conn.inserted_members[0][2] == "owner"
    assert conn.inserted_members[0][1] == uid
    assert out["user_id"] == uid
    assert out["email"] == "pat.new@gmail.com"
    assert out["org_id"] == conn.inserted_orgs[0][0]
    assert out["org_id"] != DEFAULT_ORG_ID
    assert out["org_name"] == conn.inserted_orgs[0][1]
    assert out["temporary_password"]
    payload = posted.calls[0]["json"]
    assert payload["email_confirm"] is True
    assert payload["password"] == out["temporary_password"]
    assert payload["user_metadata"] == {"first_name": "Pat", "last_name": "New"}
    assert "/auth/v1/admin/users" in posted.calls[0]["url"]


def test_new_org_uses_admin_provided_name(monkeypatch):
    uid, _posted = _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="pat.new@gmail.com",
            first_name="Pat",
            last_name="New",
            org_mode="new",
            org_name="  Acme Inc  ",
        )
    assert out["org_name"] == "Acme Inc"
    assert conn.inserted_orgs[0][1] == "Acme Inc"
    assert uid == out["user_id"]


def test_new_org_falls_back_to_workspace_name_when_org_name_omitted(monkeypatch):
    _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="pat.new@gmail.com",
            first_name="Pat",
            last_name="New",
            org_mode="new",
        )
    assert out["org_name"] == "pat.new's workspace"
    assert conn.inserted_orgs[0][1] == "pat.new's workspace"


def test_new_org_blank_org_name_falls_back_too(monkeypatch):
    _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="pat.new@gmail.com",
            first_name="Pat",
            last_name="New",
            org_mode="new",
            org_name="   ",
        )
    assert out["org_name"] == "pat.new's workspace"


def test_existing_org_ignores_org_name(monkeypatch):
    uid, _posted = _auth_ok(monkeypatch)
    conn = _FakeConn(orgs=[{"id": EXISTING_ORG, "name": "Acme"}])
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="member@gmail.com",
            first_name="Mo",
            last_name="Lee",
            org_mode="existing",
            org_id=EXISTING_ORG,
            org_name="Ignored Name",
        )
    assert conn.inserted_orgs == []
    assert out["org_name"] == "Acme"
    assert uid == out["user_id"]


def test_existing_org_creates_zero_orgs_and_member(monkeypatch):
    uid, _posted = _auth_ok(monkeypatch)
    conn = _FakeConn(orgs=[{"id": EXISTING_ORG, "name": "Acme"}])
    with _fake_db(monkeypatch, conn):
        out = provision_user(
            email="member@gmail.com",
            first_name="Mo",
            last_name="Lee",
            org_mode="existing",
            org_id=EXISTING_ORG,
        )
    assert conn.inserted_orgs == []
    assert len(conn.inserted_members) == 1
    assert conn.inserted_members[0][0] == EXISTING_ORG
    assert conn.inserted_members[0][1] == uid
    assert conn.inserted_members[0][2] == "member"
    assert out["org_id"] == EXISTING_ORG
    assert out["org_name"] == "Acme"


def test_non_admin_gets_403_and_does_not_create_user(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    posted = MagicMock()
    monkeypatch.setattr("backend.admin_provision.httpx.post", posted)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        "/api/admin/provision-user",
        json={
            "email": "new@gmail.com",
            "first_name": "A",
            "last_name": "B",
            "org_mode": "new",
        },
    )
    assert r.status_code == 403
    assert r.json() == {"detail": "Not authorized."}
    posted.assert_not_called()


def test_admin_can_provision_new_org_over_http(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    uid, _posted = _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        from backend.api import app

        client = TestClient(app)
        authorize(client, monkeypatch)
        r = client.post(
            "/api/admin/provision-user",
            json={
                "email": "trial@gmail.com",
                "first_name": "Trial",
                "last_name": "User",
                "org_mode": "new",
            },
        )
    assert r.status_code == 200
    body = r.json()
    assert body["user_id"] == uid
    assert body["temporary_password"]
    assert len(conn.inserted_orgs) == 1
    assert conn.inserted_members[0][2] == "owner"


def test_admin_can_set_org_name_over_http(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    _auth_ok(monkeypatch)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        from backend.api import app

        client = TestClient(app)
        authorize(client, monkeypatch)
        r = client.post(
            "/api/admin/provision-user",
            json={
                "email": "trial@gmail.com",
                "first_name": "Trial",
                "last_name": "User",
                "org_mode": "new",
                "org_name": "Trial Org",
            },
        )
    assert r.status_code == 200
    assert r.json()["org_name"] == "Trial Org"
    assert conn.inserted_orgs[0][1] == "Trial Org"


def test_jwt_email_not_on_allowlist_does_not_call_supabase(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "admin@example.com")
    posted = MagicMock()
    monkeypatch.setattr("backend.admin_provision.httpx.post", posted)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, sub=str(uuid.uuid4()))
    client.headers["Authorization"] = (
        f"Bearer {mint_access_token(email='tester@example.com')}"
    )
    r = client.post(
        "/api/admin/provision-user",
        json={
            "email": "new@gmail.com",
            "first_name": "A",
            "last_name": "B",
            "org_mode": "new",
        },
    )
    assert r.status_code == 403
    posted.assert_not_called()
