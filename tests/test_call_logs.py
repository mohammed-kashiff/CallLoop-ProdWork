"""Admin call-log search: email/org id/short id, owner-vs-member scoping, CSV export."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID
ORG_B = "00000000-0000-4000-8000-0000000000bb"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _CallLogsFakeConn:
    """Answers exactly the queries _resolve_call_log_scope()/_call_logs_rows() issue."""

    def __init__(self, members=None, calls=None, audit_sizes=None):
        self.members = members or []  # dicts: org_id, user_id, role, first_name, last_name, short_id, email
        self.calls = calls or []  # dicts: id, org_id, filename, job_id, created_at, audio_seconds,
        # uploaded_by, deleted_at, deleted_by, raw_json_bytes
        self.audit_sizes = audit_sizes or []  # (org_id, call_id, bytes)
        self.queries: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        params = list(params or [])
        self.queries.append((norm, params))

        if "FROM ORG_MEMBERS WHERE SHORT_ID" in norm:
            sid = params[0]
            row = next((m for m in self.members if m["short_id"] == sid), None)
            return _Result([row] if row else [])

        if "ADMIN_SEARCH_DIRECTORY" in norm:
            q = str(params[0] or "").lower()
            rows = [
                m for m in self.members
                if q in (m.get("email") or "").lower()
                or q in str(m.get("org_id") or "").lower()
                or q in str(m.get("user_id") or "").lower()
                or q in str(m.get("short_id") or "")
            ]
            return _Result(rows)

        if norm.startswith("SELECT COUNT(*) AS N FROM CALLS"):
            org_id = params[0]
            uploaded_by = params[1] if len(params) > 1 else None
            matched = [
                c for c in self.calls
                if c["org_id"] == org_id and (uploaded_by is None or c.get("uploaded_by") == uploaded_by)
            ]
            return _Result([{"n": len(matched)}])

        if norm.startswith("SELECT C.ID, C.FILENAME"):
            org_id = params[0]
            limit = params[-1]
            uploaded_by = params[1] if len(params) > 2 else None
            matched = [
                c for c in self.calls
                if c["org_id"] == org_id and (uploaded_by is None or c.get("uploaded_by") == uploaded_by)
            ]
            matched = sorted(matched, key=lambda c: c["created_at"], reverse=True)
            return _Result(matched[:limit])

        if "SUM(PG_COLUMN_SIZE(A.FINDINGS))" in norm:
            org_id = params[0]
            uploaded_by = params[1] if len(params) > 1 else None
            call_ids = {
                c["id"] for c in self.calls
                if c["org_id"] == org_id and (uploaded_by is None or c.get("uploaded_by") == uploaded_by)
            }
            totals: dict[int, int] = {}
            for oid, cid, n in self.audit_sizes:
                if oid == org_id and cid in call_ids:
                    totals[cid] = totals.get(cid, 0) + n
            return _Result([{"call_id": cid, "n": n} for cid, n in totals.items()])

        if "SHORT_ID FROM ORG_MEMBERS WHERE ORG_ID" in norm:
            org_id = params[0]
            rows = [
                {
                    "user_id": m["user_id"], "first_name": m.get("first_name"),
                    "last_name": m.get("last_name"), "short_id": m.get("short_id"),
                }
                for m in self.members if m["org_id"] == org_id
            ]
            return _Result(rows)

        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.admin_console.db.connection", _connection)
    yield conn


def _member(org_id, user_id, role, *, short_id, first="Ada", last="Lovelace", email=None):
    return {
        "org_id": org_id, "user_id": user_id, "role": role, "short_id": short_id,
        "first_name": first, "last_name": last,
        "email": email or f"{first.lower()}@example.com",
    }


def _call(cid, org_id, uploaded_by=None, *, filename="a.mp3", created=None, deleted_at=None, deleted_by=None):
    return {
        "id": cid, "org_id": org_id, "filename": filename, "job_id": "job_x",
        "created_at": created or datetime(2026, 1, cid, tzinfo=timezone.utc),
        "audio_seconds": 90, "uploaded_by": uploaded_by,
        "deleted_at": deleted_at, "deleted_by": deleted_by, "raw_json_bytes": 1000,
    }


# ---------- scope resolution ----------


def test_resolve_bare_org_id_scopes_whole_org(monkeypatch):
    from backend.admin_console import _resolve_call_log_scope

    conn = _CallLogsFakeConn()
    with _fake_db(monkeypatch, conn):
        org_id, uploaded_by, matched = _resolve_call_log_scope(ORG_A)
    assert org_id == ORG_A
    assert uploaded_by is None
    assert matched["scope"] == "org"
    assert conn.queries == []  # no lookup needed — it's already a UUID


def test_resolve_short_id_owner_scopes_whole_org(monkeypatch):
    from backend.admin_console import _resolve_call_log_scope

    owner_id = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[_member(ORG_A, owner_id, "owner", short_id=100001, first="Owen", last="Ortiz")],
    )
    with _fake_db(monkeypatch, conn):
        org_id, uploaded_by, matched = _resolve_call_log_scope("100001")
    assert org_id == ORG_A
    assert uploaded_by is None
    assert matched["scope"] == "org"
    assert matched["name"] == "Owen Ortiz"


def test_resolve_short_id_member_scopes_to_their_uploads(monkeypatch):
    from backend.admin_console import _resolve_call_log_scope

    member_id = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[_member(ORG_A, member_id, "member", short_id=100002, first="Mo", last="Member")],
    )
    with _fake_db(monkeypatch, conn):
        org_id, uploaded_by, matched = _resolve_call_log_scope("100002")
    assert org_id == ORG_A
    assert uploaded_by == member_id
    assert matched["scope"] == "member"
    assert matched["name"] == "Mo Member"


def test_resolve_email_matches_exactly_not_by_substring(monkeypatch):
    from backend.admin_console import _resolve_call_log_scope

    uid = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[
            _member(ORG_A, uid, "member", short_id=100003, first="Ada", last="Lovelace", email="ada@example.com"),
            _member(ORG_B, str(uuid.uuid4()), "owner", short_id=100004, first="Ada2", last="X", email="ada2@example.com"),
        ],
    )
    with _fake_db(monkeypatch, conn):
        org_id, uploaded_by, matched = _resolve_call_log_scope("ADA@EXAMPLE.COM")
    assert org_id == ORG_A
    assert uploaded_by == uid
    assert matched["email"] == "ada@example.com"


def test_resolve_no_match_is_404(monkeypatch):
    from fastapi import HTTPException
    import pytest

    from backend.admin_console import _resolve_call_log_scope

    conn = _CallLogsFakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            _resolve_call_log_scope("nobody@example.com")
    assert exc.value.status_code == 404


def test_resolve_empty_query_is_400():
    from fastapi import HTTPException
    import pytest

    from backend.admin_console import _resolve_call_log_scope

    with pytest.raises(HTTPException) as exc:
        _resolve_call_log_scope("   ")
    assert exc.value.status_code == 400


# ---------- call_logs() end-to-end ----------


def test_call_logs_member_scope_only_shows_their_uploads(monkeypatch):
    from backend.admin_console import call_logs

    owner_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[
            _member(ORG_A, owner_id, "owner", short_id=100001, first="Owen", last="Ortiz"),
            _member(ORG_A, member_id, "member", short_id=100002, first="Mo", last="Member"),
        ],
        calls=[
            _call(1, ORG_A, uploaded_by=owner_id, filename="owner-call.mp3"),
            _call(2, ORG_A, uploaded_by=member_id, filename="member-call.mp3"),
        ],
    )
    with _fake_db(monkeypatch, conn):
        out = call_logs("100002")
    assert out["matched"]["scope"] == "member"
    assert [c["filename"] for c in out["calls"]] == ["member-call.mp3"]
    assert out["total_calls"] == 1


def test_call_logs_owner_scope_shows_whole_org(monkeypatch):
    from backend.admin_console import call_logs

    owner_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[
            _member(ORG_A, owner_id, "owner", short_id=100001, first="Owen", last="Ortiz"),
            _member(ORG_A, member_id, "member", short_id=100002, first="Mo", last="Member"),
        ],
        calls=[
            _call(1, ORG_A, uploaded_by=owner_id, filename="owner-call.mp3"),
            _call(2, ORG_A, uploaded_by=member_id, filename="member-call.mp3"),
            _call(3, ORG_B, uploaded_by=None, filename="other-org.mp3"),
        ],
    )
    with _fake_db(monkeypatch, conn):
        out = call_logs("100001")
    assert out["matched"]["scope"] == "org"
    names = {c["filename"] for c in out["calls"]}
    assert names == {"owner-call.mp3", "member-call.mp3"}
    assert out["total_calls"] == 2


def test_call_logs_reports_deleted_flag_short_id_and_data_size(monkeypatch):
    from backend.admin_console import call_logs

    owner_id = str(uuid.uuid4())
    deleter_id = str(uuid.uuid4())
    conn = _CallLogsFakeConn(
        members=[
            _member(ORG_A, owner_id, "owner", short_id=100001, first="Owen", last="Ortiz"),
            _member(ORG_A, deleter_id, "member", short_id=100002, first="Mo", last="Member"),
        ],
        calls=[
            _call(1, ORG_A, uploaded_by=owner_id, deleted_at="2026-02-01", deleted_by=deleter_id),
        ],
        audit_sizes=[(ORG_A, 1, 200), (ORG_A, 1, 50)],
    )
    with _fake_db(monkeypatch, conn):
        out = call_logs("100001")
    row = out["calls"][0]
    assert row["deleted"] is True
    assert row["deleted_by_short_id"] == 100002
    assert row["data_size_bytes"] == 1000 + 250


# ---------- routes ----------


def test_call_logs_route_is_gated_and_forwards_query(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[tuple] = []

    def _call_logs(query, limit=None):
        seen.append((query, limit))
        return {"matched": {"org_id": ORG_A, "scope": "org"}, "calls": [], "total_calls": 0, "calls_truncated": False}

    monkeypatch.setattr("backend.api.admin_console.call_logs", _call_logs)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/call-logs", params={"query": "ada@example.com"})
    assert r.status_code == 200
    assert seen == [("ada@example.com", None)]


def test_call_logs_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/call-logs", params={"query": "x"}).status_code == 403


def test_call_logs_export_returns_csv_with_rows(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")

    def _rows(query):
        return (
            [
                {
                    "call_id": 1, "filename": "a.mp3", "created_at": "2026-01-01", "audio_seconds": 90,
                    "mode": "pyai", "uploaded_by": "Ada Lovelace", "data_size_bytes": 1234,
                    "deleted": False, "deleted_by_short_id": None,
                },
            ],
            {"org_id": ORG_A, "scope": "org"},
        )

    monkeypatch.setattr("backend.api.admin_console.call_logs_csv_rows", _rows)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/call-logs/export", params={"query": ORG_A})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert "callproof-call-logs-" in r.headers["content-disposition"]
    body = r.text
    assert "a.mp3" in body
    assert "Ada Lovelace" in body
    assert "1234" in body


def test_call_logs_export_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/call-logs/export", params={"query": "x"}).status_code == 403
