"""AC-16: password change/reset audit trail.

Investigated first: Supabase's auth.audit_log_entries has an ip_address
column but is empty for this project (verified via direct query, 0 rows
despite real activity), and Supabase Auth Hooks have no "password changed"
event to attach to (only Before User Created / Custom Access Token /
Send SMS / Send Email / MFA Verification Attempt / Password Verification
Attempt exist — the last is about sign-in, not password changes). So this
is captured ourselves: the admin reset actions (already ours) and a
self-report the frontend sends after Supabase confirms a self-service
change.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = "00000000-0000-4000-8000-0000000000aa"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, members=None, events=None):
        self.members = members or []  # (org_id, user_id)
        self.events = list(events or [])  # dicts as inserted
        self.inserts: list = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "SELECT ORG_ID FROM ORG_MEMBERS WHERE USER_ID" in norm:
            uid = params[0]
            for oid, mid in self.members:
                if mid == uid:
                    return _Result([{"org_id": oid}])
            return _Result([])
        if "INSERT INTO PASSWORD_RESET_EVENTS" in norm:
            oid, uid, event_type, actor_email, ip = params
            row = {
                "org_id": oid, "user_id": uid, "event_type": event_type,
                "actor_email": actor_email, "ip_address": ip,
                "created_at": datetime(2026, 1, 1, tzinfo=timezone.utc)
                + timedelta(seconds=len(self.events)),
            }
            self.events.append(row)
            self.inserts.append(params)
            return _Result([])
        if "FROM PASSWORD_RESET_EVENTS" in norm and "ORDER BY CREATED_AT DESC" in norm:
            oid, uid = params
            matched = [
                e for e in self.events if e["org_id"] == oid and e["user_id"] == uid
            ]
            matched.sort(key=lambda e: e["created_at"], reverse=True)
            return _Result(matched)
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.password_events.db.connection", _connection)
    yield conn


def test_record_event_resolves_org_and_writes_row(monkeypatch):
    from backend import password_events

    uid = str(uuid.uuid4())
    conn = _FakeConn(members=[(ORG_A, uid)])
    with _fake_db(monkeypatch, conn):
        password_events.record_event(
            user_id=uid, event_type="admin_reset_email",
            actor_email="Admin@Example.com", ip_address="1.2.3.4",
        )
    assert len(conn.inserts) == 1
    assert conn.inserts[0] == (ORG_A, uid, "admin_reset_email", "admin@example.com", "1.2.3.4")


def test_record_event_rejects_bad_input(monkeypatch):
    from backend import password_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            password_events.record_event(user_id="not-a-uuid", event_type="self_service")
        assert exc.value.status_code == 400
        with pytest.raises(HTTPException) as exc2:
            password_events.record_event(user_id=str(uuid.uuid4()), event_type="bogus")
        assert exc2.value.status_code == 400
    assert conn.inserts == []


def test_record_event_skips_silently_with_no_org_membership(monkeypatch):
    from backend import password_events

    conn = _FakeConn(members=[])
    with _fake_db(monkeypatch, conn):
        password_events.record_event(user_id=str(uuid.uuid4()), event_type="self_service")
    assert conn.inserts == []


def test_history_for_user_orders_most_recent_first_and_is_org_isolated(monkeypatch):
    from backend import password_events

    uid = str(uuid.uuid4())
    other_uid = str(uuid.uuid4())
    ORG_B = "00000000-0000-4000-8000-0000000000bb"
    conn = _FakeConn(members=[(ORG_A, uid), (ORG_B, other_uid)])
    with _fake_db(monkeypatch, conn):
        password_events.record_event(user_id=uid, event_type="self_service", ip_address="1.1.1.1")
        password_events.record_event(
            user_id=uid, event_type="admin_reset_email", actor_email="a@x.com", ip_address="2.2.2.2",
        )
        password_events.record_event(user_id=other_uid, event_type="self_service", ip_address="9.9.9.9")
        out = password_events.history_for_user(uid)
    assert [e["event_type"] for e in out] == ["admin_reset_email", "self_service"]
    assert all(e["ip_address"] != "9.9.9.9" for e in out)


def test_history_for_user_empty_for_unknown_user():
    from backend import password_events

    assert password_events.history_for_user("not-a-uuid") == []


def test_self_password_changed_records_authenticated_user(monkeypatch):
    monkeypatch.setattr("backend.api.password_events.org_for_user", lambda uid: ORG_A)
    seen: list[tuple] = []

    def _record(*, user_id, event_type, org_id=None, actor_email=None, ip_address=None):
        seen.append((user_id, event_type, org_id, actor_email, ip_address))

    monkeypatch.setattr("backend.api.password_events.record_event", _record)
    from backend.api import app

    client = TestClient(app)
    uid = authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/me/password-changed")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert len(seen) == 1
    assert seen[0][0] == uid
    assert seen[0][1] == "self_service"
    assert seen[0][2] == ORG_A


def test_self_password_changed_requires_auth():
    from backend.api import app

    client = TestClient(app)
    assert client.post("/api/me/password-changed").status_code == 401


def test_self_password_changed_write_failure_does_not_fail_the_request(monkeypatch):
    def _boom(**_kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr("backend.api.password_events.record_event", _boom)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/me/password-changed")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_admin_password_events_route_is_gated_and_scoped(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    uid = str(uuid.uuid4())
    seen: list[str] = []

    def _history(user_id):
        seen.append(user_id)
        return [{"event_type": "self_service", "actor_email": None,
                  "ip_address": "1.2.3.4", "created_at": "2026-01-01T00:00:00"}]

    monkeypatch.setattr("backend.api.password_events.history_for_user", _history)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get(f"/api/admin/users/{uid}/password-events")
    assert r.status_code == 200
    assert seen == [uid]
    assert r.json()["events"][0]["event_type"] == "self_service"


def test_admin_password_events_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert (
        client.get(f"/api/admin/users/{uuid.uuid4()}/password-events").status_code == 403
    )


def test_admin_password_events_route_rejects_bad_user_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/users/not-a-uuid/password-events").status_code == 400


def test_log_password_reset_request_also_records_password_event(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    uid = str(uuid.uuid4())
    seen: list[tuple] = []

    def _record(*, user_id, event_type, org_id=None, actor_email=None, ip_address=None):
        seen.append((user_id, event_type, actor_email))

    monkeypatch.setattr("backend.api.password_events.record_event", _record)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        "/api/admin/log-password-reset-request",
        json={"user_id": uid, "email": "ada@example.com"},
    )
    assert r.status_code == 200
    assert seen == [(uid, "admin_reset_email", "tester@example.com")]


def test_log_password_reset_request_still_succeeds_if_event_write_fails(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")

    def _boom(**_kwargs):
        raise RuntimeError("db is down")

    monkeypatch.setattr("backend.api.password_events.record_event", _boom)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        "/api/admin/log-password-reset-request",
        json={"user_id": str(uuid.uuid4()), "email": "ada@example.com"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_password_events_module_does_not_bypass_rls():
    src = (ROOT / "backend" / "password_events.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "GRANT" not in src


def test_eighteenth_revision_password_reset_events_is_append_only():
    rev = ROOT / "alembic" / "versions" / "0018_password_reset_events.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE PASSWORD_RESET_EVENTS" in sql
    assert "REFERENCES ORG_MEMBERS (USER_ID)" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY PASSWORD_RESET_EVENTS_SELECT" in sql
    assert "CREATE POLICY PASSWORD_RESET_EVENTS_INSERT" in sql
    assert "CREATE POLICY PASSWORD_RESET_EVENTS_UPDATE" not in sql
    assert "CREATE POLICY PASSWORD_RESET_EVENTS_DELETE" not in sql
    assert "GRANT SELECT, INSERT ON PASSWORD_RESET_EVENTS TO CALLPROOF_APP" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "bypass_rls" not in raw
    assert "0017_call_audit_attribution" in raw
