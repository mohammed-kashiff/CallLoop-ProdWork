"""AC-24: per-call pipeline audit trail. Best-effort by design — a trail
write failure must never break the pipeline step it describes."""

from __future__ import annotations

import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserts: list = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "INSERT INTO CALL_PIPELINE_EVENTS" in norm:
            oid, call_id, stage, status, detail, error = params
            row = {
                "org_id": oid, "call_id": call_id, "stage": stage, "status": status,
                "detail": detail, "error": error,
                "created_at": None,
            }
            self.rows.append(row)
            self.inserts.append(params)
            return _Result([])
        if "FROM CALL_PIPELINE_EVENTS" in norm:
            call_id, oid = params
            matched = [
                r for r in self.rows if r["call_id"] == call_id and r["org_id"] == oid
            ]
            return _Result(matched)
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.call_trail.db.connection", _cm)
    yield conn


def test_record_writes_a_row_with_org_scoped_insert(monkeypatch):
    from backend import call_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        call_trail.record(
            624, ORG_A, "transcription", "succeeded",
            detail={"mode": "channel", "segments": 89},
        )
    assert len(conn.inserts) == 1
    oid, call_id, stage, status, detail, error = conn.inserts[0]
    assert oid == ORG_A
    assert call_id == 624
    assert stage == "transcription"
    assert status == "succeeded"
    assert json.loads(detail) == {"mode": "channel", "segments": 89}
    assert error is None


def test_record_stores_error_only_on_failed_rows(monkeypatch):
    from backend import call_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        call_trail.record(624, ORG_A, "scoring", "failed", error="no_segments")
    assert conn.inserts[0][3] == "failed"
    assert conn.inserts[0][5] == "no_segments"


def test_record_coerces_an_invalid_status_to_failed_rather_than_dropping_it(monkeypatch):
    from backend import call_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        call_trail.record(624, ORG_A, "transcription", "bogus_status")
    assert len(conn.inserts) == 1
    assert conn.inserts[0][3] == "failed"
    assert json.loads(conn.inserts[0][4])["_invalid_status"] == "bogus_status"


def test_record_skips_silently_with_no_org_id(monkeypatch):
    from backend import call_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        call_trail.record(624, None, "transcription", "succeeded")
    assert conn.inserts == []


def test_record_never_raises_when_the_write_itself_fails(monkeypatch):
    """The whole point: a trail-write failure must not break the caller."""
    from backend import call_trail

    @contextmanager
    def _boom(*_a, **_k):
        raise RuntimeError("db is down")
        yield  # pragma: no cover

    monkeypatch.setattr("backend.call_trail.db.connection", _boom)
    call_trail.record(624, ORG_A, "transcription", "succeeded")  # must not raise


def test_history_returns_rows_in_chronological_order(monkeypatch):
    from backend import call_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        call_trail.record(624, ORG_A, "transcription", "succeeded", detail={"mode": "channel"})
        call_trail.record(624, ORG_A, "scoring", "started")
        out = call_trail.history(624, ORG_A)
    assert [e["stage"] for e in out] == ["transcription", "scoring"]
    assert out[0]["detail"] == {"mode": "channel"}


def test_history_empty_for_bad_org_id():
    from backend import call_trail

    assert call_trail.history(624, "not-a-uuid") == []


# ---------- GET /api/admin/calls/{call_id}/trail ----------


def test_admin_trail_route_scopes_by_caller_supplied_org_and_returns_history(monkeypatch):
    """org_id comes from the caller (Call Logs already has it), not resolved
    server-side — this route must never bypass RLS (see test_rls.py)."""
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen_scopes: list[str] = []

    class _CallLookupConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            assert params == (624, ORG_A)
            return _Result([{"filename": "recording (31).mp3"}])

    @contextmanager
    def _fake_org_scope(oid):
        seen_scopes.append(oid)
        yield

    from backend import api

    monkeypatch.setattr(api, "org_scope", _fake_org_scope)
    monkeypatch.setattr(api.db, "connection", lambda **kw: _CallLookupConn())
    monkeypatch.setattr(
        api.call_trail, "history",
        lambda call_id, org_id: [{"stage": "transcription", "status": "succeeded"}],
    )
    client = TestClient(api.app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/calls/624/trail", params={"org_id": ORG_A})
    assert r.status_code == 200
    body = r.json()
    assert body["call_id"] == 624
    assert body["org_id"] == ORG_A
    assert body["filename"] == "recording (31).mp3"
    assert body["events"] == [{"stage": "transcription", "status": "succeeded"}]
    assert seen_scopes == [ORG_A]


def test_admin_trail_route_requires_org_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/calls/624/trail").status_code in (400, 422)
    assert client.get(
        "/api/admin/calls/624/trail", params={"org_id": "not-a-uuid"},
    ).status_code == 400


def test_admin_trail_route_404s_for_unknown_call(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")

    class _EmptyConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            return _Result([])

    from backend import api

    monkeypatch.setattr(api.db, "connection", lambda **kw: _EmptyConn())
    client = TestClient(api.app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/calls/999999/trail", params={"org_id": ORG_A})
    assert r.status_code == 404


def test_admin_trail_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/calls/624/trail", params={"org_id": ORG_A})
    assert r.status_code == 403


def test_admin_call_trail_route_never_bypasses_rls():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def admin_call_trail(")
    end = src.index("\n@app.", start + 1)
    region = src[start:end]
    assert "bypass_rls" not in region


# ---------- qa_v8.run_v8_wave on_dimension_event hook ----------


def test_run_v8_wave_notifies_started_and_succeeded_per_dimension():
    from backend import qa_v8

    definition = {
        "technical_skills": {"bucket_weight": 100, "dimensions": [
            {"id": "active_listening", "name": "Active Listening",
             "method": "deterministic", "weight": 100},
        ]},
        "soft_skills": {"bucket_weight": 0, "dimensions": []},
    }

    events: list[tuple] = []

    def _on_event(dim, status, detail):
        events.append((dim.get("id"), status))

    def _assess_churn(*_a, **_k):
        return {"risk": "none", "reasoning": "", "evidence_seq": None, "evidence_text": None}

    qa_v8.run_v8_wave(
        definition, [], "speaker_1", "",
        call_claude=lambda *a, **k: "{}",
        parse_json=lambda s: {},
        build_prompt=lambda *a, **k: "",
        validate_evidence=lambda *a, **k: True,
        assess_churn=_assess_churn,
        max_workers=2,
    )
    # No on_dimension_event passed: must not raise (default None, backward compatible).

    events.clear()
    qa_v8.run_v8_wave(
        definition, [], "speaker_1", "",
        call_claude=lambda *a, **k: "{}",
        parse_json=lambda s: {},
        build_prompt=lambda *a, **k: "",
        validate_evidence=lambda *a, **k: True,
        assess_churn=_assess_churn,
        max_workers=2,
        on_dimension_event=_on_event,
    )
    statuses = [s for _id, s in events]
    assert "started" in statuses
    assert "succeeded" in statuses or "failed" in statuses


def test_on_dimension_event_exception_never_breaks_scoring():
    from backend import qa_v8

    definition = {
        "technical_skills": {"bucket_weight": 100, "dimensions": [
            {"id": "active_listening", "name": "Active Listening",
             "method": "deterministic", "weight": 100},
        ]},
        "soft_skills": {"bucket_weight": 0, "dimensions": []},
    }

    def _boom(dim, status, detail):
        raise RuntimeError("callback exploded")

    def _assess_churn(*_a, **_k):
        return {"risk": "none", "reasoning": "", "evidence_seq": None, "evidence_text": None}

    # Must not raise even though the callback always raises.
    out = qa_v8.run_v8_wave(
        definition, [], "speaker_1", "",
        call_claude=lambda *a, **k: "{}",
        parse_json=lambda s: {},
        build_prompt=lambda *a, **k: "",
        validate_evidence=lambda *a, **k: True,
        assess_churn=_assess_churn,
        max_workers=2,
        on_dimension_event=_boom,
    )
    assert out[0] is not None


def test_twenty_first_revision_call_pipeline_events_is_append_only():
    rev = ROOT / "alembic" / "versions" / "0021_call_pipeline_events.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE CALL_PIPELINE_EVENTS" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY CALL_PIPELINE_EVENTS_SELECT" in sql
    assert "CREATE POLICY CALL_PIPELINE_EVENTS_INSERT" in sql
    assert "CREATE POLICY CALL_PIPELINE_EVENTS_UPDATE" not in sql
    assert "CREATE POLICY CALL_PIPELINE_EVENTS_DELETE" not in sql
    assert "GRANT SELECT, INSERT ON CALL_PIPELINE_EVENTS TO CALLPROOF_APP" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "bypass_rls" not in raw
    assert "0020_impersonation_log" in raw


def test_call_trail_module_does_not_bypass_rls():
    src = (ROOT / "backend" / "call_trail.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "GRANT" not in src
