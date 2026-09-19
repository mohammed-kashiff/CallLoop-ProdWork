"""Ticket pipeline trail: best-effort ingest/score events, Command Center viewer."""

from __future__ import annotations

import json
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID
TICKET_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


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
        if "INSERT INTO TICKET_PIPELINE_EVENTS" in norm:
            oid, ticket_id, stage, status, detail, error = params
            row = {
                "org_id": oid, "ticket_id": ticket_id, "stage": stage, "status": status,
                "detail": detail, "error": error,
                "created_at": None,
            }
            self.rows.append(row)
            self.inserts.append(params)
            return _Result([])
        if "FROM TICKET_PIPELINE_EVENTS" in norm:
            ticket_id, oid = params
            matched = [
                r for r in self.rows
                if r["ticket_id"] == ticket_id and r["org_id"] == oid
            ]
            return _Result(matched)
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.ticket_trail.db.connection", _cm)
    yield conn


def test_record_writes_a_row_with_org_scoped_insert(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record(
            TICKET_A, ORG_A, "parse", "succeeded",
            detail={"source": "pdf_upload", "turns": 4},
        )
    assert len(conn.inserts) == 1
    oid, ticket_id, stage, status, detail, error = conn.inserts[0]
    assert oid == ORG_A
    assert ticket_id == TICKET_A
    assert stage == "parse"
    assert status == "succeeded"
    assert json.loads(detail) == {"source": "pdf_upload", "turns": 4}
    assert error is None


def test_record_stores_error_only_on_failed_rows(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record(TICKET_A, ORG_A, "scoring", "failed", error="no_messages")
    assert conn.inserts[0][3] == "failed"
    assert conn.inserts[0][5] == "no_messages"


def test_record_coerces_an_invalid_status_to_failed_rather_than_dropping_it(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record(TICKET_A, ORG_A, "parse", "bogus_status")
    assert len(conn.inserts) == 1
    assert conn.inserts[0][3] == "failed"
    assert json.loads(conn.inserts[0][4])["_invalid_status"] == "bogus_status"


def test_record_skips_silently_with_no_org_id(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record(TICKET_A, None, "parse", "succeeded")
    assert conn.inserts == []


def test_record_skips_silently_with_no_ticket_id(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record("not-a-uuid", ORG_A, "parse", "succeeded")
    assert conn.inserts == []


def test_record_never_raises_when_the_write_itself_fails(monkeypatch):
    from backend import ticket_trail

    @contextmanager
    def _boom(*_a, **_k):
        raise RuntimeError("db is down")
        yield  # pragma: no cover

    monkeypatch.setattr("backend.ticket_trail.db.connection", _boom)
    ticket_trail.record(TICKET_A, ORG_A, "parse", "succeeded")


def test_history_returns_rows_in_chronological_order(monkeypatch):
    from backend import ticket_trail

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_trail.record(TICKET_A, ORG_A, "parse", "succeeded", detail={"source": "pdf_upload"})
        ticket_trail.record(TICKET_A, ORG_A, "scoring", "started")
        out = ticket_trail.history(TICKET_A, ORG_A)
    assert [e["stage"] for e in out] == ["parse", "scoring"]
    assert out[0]["detail"] == {"source": "pdf_upload"}


def test_history_empty_for_bad_org_id():
    from backend import ticket_trail

    assert ticket_trail.history(TICKET_A, "not-a-uuid") == []


def test_agent_resolve_detail_counts_unique_ids_and_names():
    from backend import ticket_trail

    turns = [
        {"speaker": "agent", "agent_user_id": "u1", "speaker_name": "Ada"},
        {"speaker": "agent", "agent_user_id": "u1", "speaker_name": "Ada"},
        {"speaker": "agent", "agent_user_id": None, "speaker_name": "Kashif"},
        {"speaker": "customer", "agent_user_id": None, "speaker_name": "Kevin"},
    ]
    assert ticket_trail.agent_resolve_detail(turns) == {"resolved": 1, "unresolved": 1}


def test_with_apis_strips_query_strings_and_skips_empty():
    from backend import ticket_trail

    out = ticket_trail.with_apis(
        {"count": 1},
        {"method": "get", "endpoint": "https://cdn.example/file.png?token=secret"},
        {"method": "", "endpoint": "https://example.com"},
        {"method": "POST", "endpoint": "https://api.anthropic.com/v1/messages"},
    )
    assert out["count"] == 1
    assert out["apis"] == [
        {"method": "GET", "endpoint": "https://cdn.example/file.png"},
        {"method": "POST", "endpoint": "https://api.anthropic.com/v1/messages"},
    ]
    assert "token" not in json.dumps(out)


def test_intercom_fetch_apis_includes_linked_kinds():
    from backend import ticket_trail

    assert ticket_trail.intercom_fetch_apis("conversation") == (
        ticket_trail.API_INTERCOM_CONVERSATION,
    )
    assert ticket_trail.intercom_fetch_apis("ticket") == (
        ticket_trail.API_INTERCOM_TICKET,
    )
    both = ticket_trail.intercom_fetch_apis("conversation", [("ticket", "t-1")])
    assert ticket_trail.API_INTERCOM_CONVERSATION in both
    assert ticket_trail.API_INTERCOM_TICKET in both


def test_admin_trail_route_scopes_by_caller_supplied_org_and_returns_history(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen_scopes: list[str] = []

    class _TicketLookupConn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            assert params == (TICKET_A, ORG_A)
            return _Result([{"source": "pdf_upload", "subject": "Checkout 504"}])

    @contextmanager
    def _fake_org_scope(oid):
        seen_scopes.append(oid)
        yield

    from backend import api

    monkeypatch.setattr(api, "org_scope", _fake_org_scope)
    monkeypatch.setattr(api.db, "connection", lambda **kw: _TicketLookupConn())
    monkeypatch.setattr(
        api.ticket_trail, "history",
        lambda ticket_id, org_id: [{"stage": "parse", "status": "succeeded"}],
    )
    client = TestClient(api.app)
    authorize(client, monkeypatch)
    r = client.get(f"/api/admin/tickets/{TICKET_A}/trail", params={"org_id": ORG_A})
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == TICKET_A
    assert body["org_id"] == ORG_A
    assert body["subject"] == "Checkout 504"
    assert body["source"] == "pdf_upload"
    assert body["events"] == [{"stage": "parse", "status": "succeeded"}]
    assert seen_scopes == [ORG_A]


def test_admin_trail_route_requires_org_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get(f"/api/admin/tickets/{TICKET_A}/trail").status_code in (400, 422)
    assert client.get(
        f"/api/admin/tickets/{TICKET_A}/trail", params={"org_id": "not-a-uuid"},
    ).status_code == 400


def test_admin_trail_route_400_for_invalid_ticket_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/tickets/not-a-uuid/trail", params={"org_id": ORG_A})
    assert r.status_code == 400


def test_admin_trail_route_404s_for_unknown_ticket(monkeypatch):
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
    r = client.get(f"/api/admin/tickets/{TICKET_A}/trail", params={"org_id": ORG_A})
    assert r.status_code == 404


def test_admin_trail_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get(f"/api/admin/tickets/{TICKET_A}/trail", params={"org_id": ORG_A})
    assert r.status_code == 403


def test_admin_ticket_trail_route_never_bypasses_rls():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def admin_ticket_trail(")
    end = src.index("\n@app.", start + 1)
    region = src[start:end]
    assert "bypass_rls" not in region


def test_ticket_logs_route_is_gated_and_forwards_query(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[tuple] = []

    def _ticket_logs(query, limit=None):
        seen.append((query, limit))
        return {
            "matched": {"org_id": ORG_A, "scope": "org"},
            "tickets": [],
            "total_tickets": 0,
            "tickets_truncated": False,
        }

    monkeypatch.setattr("backend.api.admin_console.ticket_logs", _ticket_logs)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ticket-logs", params={"query": "ada@example.com"})
    assert r.status_code == 200
    assert seen == [("ada@example.com", None)]
    assert r.json()["total_tickets"] == 0


def test_ticket_logs_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/ticket-logs", params={"query": "x"}).status_code == 403


def test_revision_ticket_pipeline_events_is_append_only():
    rev = ROOT / "alembic" / "versions" / "0045_ticket_pipeline_events.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE TICKET_PIPELINE_EVENTS" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY TICKET_PIPELINE_EVENTS_SELECT" in sql
    assert "CREATE POLICY TICKET_PIPELINE_EVENTS_INSERT" in sql
    assert "CREATE POLICY TICKET_PIPELINE_EVENTS_UPDATE" not in sql
    assert "CREATE POLICY TICKET_PIPELINE_EVENTS_DELETE" not in sql
    assert "GRANT SELECT, INSERT ON TICKET_PIPELINE_EVENTS TO CALLPROOF_APP" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "bypass_rls" not in raw
    assert "0044_performance_kpis" in raw
    assert "REFERENCES TICKETS (ID) ON DELETE CASCADE" in sql


def test_ticket_trail_module_does_not_bypass_rls():
    src = (ROOT / "backend" / "ticket_trail.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "GRANT" not in src
    assert "transcript" not in src.lower() or "must not contain transcript" in src


def test_run_ticket_wave_notifies_started_and_succeeded_per_dimension():
    from backend.ticket_scoring import agent_spans, run_ticket_wave_for_agent

    turns = [
        {"seq": 0, "speaker": "customer", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a", "text": "I can help with that."},
    ]
    events: list[tuple] = []

    def _on_event(dim, status, detail):
        events.append((dim.get("id"), status, detail.get("agent_user_id"), detail.get("verdict")))

    def _claude(_prompt: str) -> str:
        return json.dumps({
            "verdict": "pass", "reasoning": "ok",
            "evidence_quote": "I can help with that.", "evidence_seq": 1,
        })

    dims = [{"id": "tone", "name": "Tone", "weight": 15, "question": "Was the tone professional?"}]
    run_ticket_wave_for_agent(
        turns, dims,
        target_agent_user_id="agent-a", spans=agent_spans(turns),
        call_claude_fn=_claude,
        on_dimension_event=_on_event,
    )
    assert ("tone", "started", "agent-a", None) in events
    assert ("tone", "succeeded", "agent-a", "pass") in events
    assert not any("I can help" in json.dumps(e) for e in events)


def test_on_dimension_event_exception_never_breaks_ticket_scoring():
    from backend.ticket_scoring import agent_spans, run_ticket_wave_for_agent

    turns = [
        {"seq": 0, "speaker": "customer", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a", "text": "I can help with that."},
    ]

    def _boom(_dim, _status, _detail):
        raise RuntimeError("callback exploded")

    def _claude(_prompt: str) -> str:
        return json.dumps({
            "verdict": "pass", "reasoning": "ok",
            "evidence_quote": "I can help with that.", "evidence_seq": 1,
        })

    dims = [{"id": "tone", "name": "Tone", "weight": 15, "question": "Was the tone professional?"}]
    out = run_ticket_wave_for_agent(
        turns, dims,
        target_agent_user_id="agent-a", spans=agent_spans(turns),
        call_claude_fn=_claude,
        on_dimension_event=_boom,
    )
    assert out[0]["verdict"] == "pass"


def test_score_route_records_scoring_failed_when_ingest_failed(auth_client, monkeypatch):
    seen: list[tuple] = []

    def _record(ticket_id, org_id, stage, status, *, detail=None, error=None):
        seen.append((stage, status, error, detail))

    monkeypatch.setattr("backend.ticket_score_api.ticket_trail.record", _record)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: {
            "id": TICKET_A, "source": "pdf_upload", "status": "failed",
            "created_at": "2026-09-05T00:00:00+00:00",
            "messages": [], "assets": [], "audits": [],
        },
    )
    r = auth_client.post(f"/api/tickets/{TICKET_A}/score")
    assert r.status_code == 400
    assert any(s == "scoring" and st == "started" and e is None for s, st, e, _d in seen)
    failed = next(d for s, st, _e, d in seen if s == "scoring" and st == "failed")
    assert failed["apis"] == [{
        "method": "POST", "endpoint": "/api/tickets/{ticket_id}/score",
    }]


def test_score_route_404_does_not_write_a_trail(auth_client, monkeypatch):
    seen: list = []
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_trail.record",
        lambda *a, **k: seen.append((a, k)),
    )
    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: None)
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 404
    assert seen == []
