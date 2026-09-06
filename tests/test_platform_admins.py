"""Platform admin allowlist (0026): a DB-managed extension of the static
PLATFORM_ADMIN_EMAILS env var, reachable only through four SECURITY
DEFINER SQL functions — platform_admins itself is never granted to
callproof_app. This must never be reachable by a customer, even an org
owner, which is why every test here checks both "member" and "owner"
roles get 403, not just an unauthenticated caller."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token

ORG_A = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self):
        self.admins: dict[str, dict] = {}

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("SELECT * FROM PUBLIC.LIST_PLATFORM_ADMINS"):
            rows = [
                {"email": e, "added_by": v["added_by"], "created_at": None}
                for e, v in sorted(self.admins.items())
            ]
            return _Result(rows)
        if norm.startswith("SELECT PUBLIC.ADD_PLATFORM_ADMIN"):
            email, added_by = params
            self.admins.setdefault(email, {"added_by": added_by})
            return _Result([])
        if norm.startswith("SELECT PUBLIC.REMOVE_PLATFORM_ADMIN"):
            (email,) = params
            self.admins.pop(email, None)
            return _Result([])
        if norm.startswith("SELECT PUBLIC.IS_PLATFORM_ADMIN_EMAIL"):
            (email,) = params
            return _Result([{"v": email in self.admins}])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.platform_admins.db.connection", _cm)
    yield conn


# ---------- backend/platform_admins.py: unit tests ----------


def test_add_platform_admin_normalizes_and_stores(monkeypatch):
    from backend import platform_admins

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        result = platform_admins.add_platform_admin("  Ada@Example.COM  ", added_by="me@x.com")
    assert result == {"email": "ada@example.com", "added_by": "me@x.com"}
    assert "ada@example.com" in conn.admins


def test_add_platform_admin_rejects_invalid_email(monkeypatch):
    from backend import platform_admins
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            platform_admins.add_platform_admin("not-an-email", added_by="me@x.com")
    assert exc.value.status_code == 400


def test_list_platform_admins_returns_stored_rows(monkeypatch):
    from backend import platform_admins

    conn = _FakeConn()
    conn.admins["ada@example.com"] = {"added_by": "me@x.com"}
    with _fake_db(monkeypatch, conn):
        result = platform_admins.list_platform_admins()
    assert result == [{"email": "ada@example.com", "added_by": "me@x.com", "created_at": None}]


def test_remove_platform_admin_deletes(monkeypatch):
    from backend import platform_admins

    conn = _FakeConn()
    conn.admins["ada@example.com"] = {"added_by": "me@x.com"}
    with _fake_db(monkeypatch, conn):
        platform_admins.remove_platform_admin("Ada@Example.com")
    assert "ada@example.com" not in conn.admins


def test_module_never_bypasses_rls():
    src = (ROOT / "backend" / "platform_admins.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src


# ---------- auth.py: DB flag ORed with the env-var allowlist ----------


def test_is_platform_admin_true_via_db_flag_even_without_env_var(monkeypatch):
    """The core promise of 0026: an admin granted only through the DB
    table (never in PLATFORM_ADMIN_EMAILS) must still pass
    require_platform_admin — resolved once in ensure_membership()."""
    from backend import auth

    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)

    class _Req:
        class state:
            email = "db-admin@example.com"
            is_platform_admin_db = True

    assert auth.is_platform_admin(_Req()) is True


def test_is_platform_admin_false_when_neither_env_nor_db(monkeypatch):
    from backend import auth

    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)

    class _Req:
        class state:
            email = "nobody@example.com"
            is_platform_admin_db = False

    assert auth.is_platform_admin(_Req()) is False


def test_ensure_membership_resolves_db_admin_flag(monkeypatch):
    """A member row already exists; ensure_membership must still check
    is_platform_admin_email() and carry the result on Membership."""
    from backend import auth

    uid = str(uuid.uuid4())

    class _Row:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _Conn:
        def execute(self, sql, params=None):
            norm = " ".join(str(sql).split()).upper()
            if "SELECT ORG_ID, ROLE FROM ORG_MEMBERS" in norm:
                return _Row({"org_id": ORG_A, "role": "member"})
            if "IS_PLATFORM_ADMIN_EMAIL" in norm:
                return _Row({"v": True})
            return _Row(None)

    @contextmanager
    def _cm(*_a, **_k):
        yield _Conn()

    monkeypatch.setattr("backend.db.connection", _cm)
    monkeypatch.setattr("backend.auth.audit_store.seed_legacy_rubric", lambda *a, **k: None)

    membership = auth.ensure_membership(uid, "staff@calloop.internal")
    assert membership.is_platform_admin is True
    assert membership.role == "member"


# ---------- HTTP layer: /api/admin/platform-admins ----------


def test_all_platform_admin_routes_403_for_a_member(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app
    from backend.auth import Membership

    client = TestClient(app)
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id),
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    assert client.get("/api/admin/platform-admins").status_code == 403
    assert client.post("/api/admin/platform-admins", json={"email": "x@y.com"}).status_code == 403
    assert client.delete("/api/admin/platform-admins/x@y.com").status_code == 403


def test_all_platform_admin_routes_403_for_an_org_owner(monkeypatch):
    """An org owner is still a customer — not a platform admin. This is
    the exact distinction the feature must never blur."""
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)  # conftest's authorize() is role="owner"
    assert client.get("/api/admin/platform-admins").status_code == 403
    assert client.post("/api/admin/platform-admins", json={"email": "x@y.com"}).status_code == 403
    assert client.delete("/api/admin/platform-admins/x@y.com").status_code == 403


def test_platform_admin_can_list_and_add(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.platform_admins.list_platform_admins",
        lambda: [{"email": "ada@example.com", "added_by": "tester@example.com", "created_at": None}],
    )
    r = client.get("/api/admin/platform-admins")
    assert r.status_code == 200
    assert r.json()["admins"][0]["email"] == "ada@example.com"

    calls = []
    monkeypatch.setattr(
        "backend.platform_admins.add_platform_admin",
        lambda email, *, added_by: calls.append((email, added_by))
        or {"email": email, "added_by": added_by},
    )
    r = client.post("/api/admin/platform-admins", json={"email": "new@example.com"})
    assert r.status_code == 200
    assert calls == [("new@example.com", "tester@example.com")]


def test_platform_admin_cannot_remove_their_own_access(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.delete("/api/admin/platform-admins/tester@example.com")
    assert r.status_code == 400


def test_platform_admin_can_remove_someone_else(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.platform_admins.remove_platform_admin",
        lambda email: calls.append(email),
    )
    r = client.delete("/api/admin/platform-admins/someone-else@example.com")
    assert r.status_code == 200
    assert calls == ["someone-else@example.com"]


def test_unauthenticated_gets_401_not_403():
    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/admin/platform-admins")
    assert r.status_code == 401


# ---------- live Postgres: real end-to-end + the DB-level wall itself ----------


def test_platform_admins_live_end_to_end():
    """Real Postgres: add -> is_platform_admin_email() sees it ->
    ensure_membership() carries it -> list shows it -> remove -> gone.
    Also proves the hard DB wall: callproof_app has zero grants on the
    platform_admins table itself, only EXECUTE on the four functions."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import auth, platform_admins

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        "SELECT to_regclass('public.platform_admins') AS t"
    ).fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0026_platform_admins not applied")

    test_email = f"ta15-live-test-{uuid.uuid4().hex[:8]}@example.com"
    try:
        assert not platform_admins.list_platform_admins() or all(
            a["email"] != test_email for a in platform_admins.list_platform_admins()
        )

        with auth.db.connection() as conn:
            before = auth._resolve_is_platform_admin(conn, test_email)
        assert before is False

        platform_admins.add_platform_admin(test_email, added_by="live-test")

        with auth.db.connection() as conn:
            after = auth._resolve_is_platform_admin(conn, test_email)
        assert after is True

        admins = platform_admins.list_platform_admins()
        assert any(a["email"] == test_email for a in admins)

        # The DB-level wall: callproof_app must have NO privileges on the
        # table itself, only EXECUTE on the four SECURITY DEFINER functions.
        grants = admin.execute(
            """
            SELECT privilege_type FROM information_schema.table_privileges
            WHERE table_name = 'platform_admins' AND grantee = 'callproof_app'
            """
        ).fetchall()
        assert grants == []

        platform_admins.remove_platform_admin(test_email)
        with auth.db.connection() as conn:
            gone = auth._resolve_is_platform_admin(conn, test_email)
        assert gone is False
    finally:
        admin.execute("DELETE FROM platform_admins WHERE email = %s", (test_email,))
        admin.commit()
        admin.close()
