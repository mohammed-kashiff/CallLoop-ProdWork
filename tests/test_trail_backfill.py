"""Collapsed trail backfill from stored scorecards — skip live trails."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from backend.trail_backfill import (
    SOURCE,
    _CALLS_SQL,
    _INSERT_CALL,
    _INSERT_TICKET,
    _TICKETS_SQL,
    backfill,
    call_events_from_scorecard,
    stamp,
    ticket_events_from_scorecards,
)

ORG_A = DEFAULT_ORG_ID
TICKET_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, *, call_rows=None, ticket_rows=None):
        self.call_rows = list(call_rows or [])
        self.ticket_rows = list(ticket_rows or [])
        self.inserts: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "FROM AUDITS" in norm:
            return _Result(self.call_rows)
        if "FROM TICKET_AUDITS" in norm:
            return _Result(self.ticket_rows)
        if "INSERT INTO CALL_PIPELINE_EVENTS" in norm:
            self.inserts.append(("call", params))
            return _Result([])
        if "INSERT INTO TICKET_PIPELINE_EVENTS" in norm:
            self.inserts.append(("ticket", params))
            return _Result([])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*, bypass_rls=False):
        assert bypass_rls is True
        yield conn

    monkeypatch.setattr("backend.trail_backfill.db.connection", _cm)
    yield conn


def test_stamp_marks_reconstructed():
    out = stamp({"verdict": "pass"})
    assert out["source"] == SOURCE
    assert out["reconstructed"] is True
    assert out["verdict"] == "pass"


def test_call_events_are_collapsed_outcome_without_quotes_or_apis():
    payload = {
        "score": 82,
        "grade": "B",
        "flagged": False,
        "findings": [
            {"id": "greeting", "verdict": "pass", "evidence_text": "hello there", "reasoning": "quote"},
            {"id": "resolution", "verdict": "fail", "why": "missed the close"},
        ],
        "recap": {"status": "ok", "summary": "a long recap body"},
    }
    events = call_events_from_scorecard(payload, score=82)
    stages = [e[0] for e in events]
    assert stages[0] == "transcription"
    assert "criterion:greeting" in stages
    assert "criterion:resolution" in stages
    assert "recap" in stages
    assert stages[-1] == "scoring"
    assert all(e[1] == "succeeded" for e in events)
    scoring = events[-1][2]
    assert scoring["score"] == 82
    assert scoring["grade"] == "B"
    assert scoring["source"] == SOURCE
    greeting = next(e[2] for e in events if e[0] == "criterion:greeting")
    assert greeting == stamp({"verdict": "pass"})
    assert "evidence_text" not in greeting
    assert "reasoning" not in greeting
    assert "apis" not in greeting
    recap = next(e[2] for e in events if e[0] == "recap")
    assert recap["status"] == "ok"
    assert "summary" not in recap


def test_call_events_skip_recap_when_scorecard_has_none():
    events = call_events_from_scorecard({"score": 10, "findings": []})
    assert [e[0] for e in events] == ["transcription", "scoring"]


def test_call_events_criterion_error_is_failed_without_dumping_reasoning():
    events = call_events_from_scorecard({
        "findings": [{"id": "tone", "verdict": "error", "reasoning": "full model dump"}],
    })
    stage, status, detail, error = next(e for e in events if e[0] == "criterion:tone")
    assert status == "failed"
    assert error == "criterion_error"
    assert "full model dump" not in json.dumps(detail)


def test_ticket_events_include_parse_resolve_and_per_agent_criteria():
    events = ticket_events_from_scorecards([
        {
            "agent_user_id": "11111111-1111-1111-1111-111111111111",
            "score": 70,
            "payload": {
                "findings": [{"id": "empathy", "verdict": "pass"}],
            },
        },
        {
            "agent_user_id": "22222222-2222-2222-2222-222222222222",
            "score": 40,
            "payload": {
                "findings": [{"id": "empathy", "verdict": "fail"}],
            },
        },
    ])
    stages = [e[0] for e in events]
    assert stages[0] == "parse"
    assert stages[1] == "agent_resolve"
    assert stages.count("criterion:empathy") == 2
    assert stages[-1] == "scoring"
    empathy = [e[2] for e in events if e[0] == "criterion:empathy"]
    assert {d["agent_user_id"] for d in empathy} == {
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    }
    scoring = events[-1][2]
    assert scoring["scored"] == [
        "11111111-1111-1111-1111-111111111111",
        "22222222-2222-2222-2222-222222222222",
    ]
    assert "cached" not in scoring
    assert "apis" not in scoring


def test_queries_are_parameterized_and_skip_existing_trails():
    assert "%s" in _INSERT_CALL
    assert "%s" in _INSERT_TICKET
    assert "NOT EXISTS" in _CALLS_SQL
    assert "NOT EXISTS" in _TICKETS_SQL
    assert "deleted_at IS NULL" in _CALLS_SQL
    src = (ROOT / "backend" / "trail_backfill.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" in src
    assert "FROM audits a" in src
    assert "evidence_text" not in src


def test_backfill_inserts_calls_and_skips_when_dry_run(monkeypatch):
    scored_at = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    conn = _FakeConn(call_rows=[{
        "org_id": ORG_A,
        "call_id": 624,
        "score": 90,
        "findings": {"score": 90, "grade": "A", "findings": [{"id": "greeting", "verdict": "pass"}]},
        "scored_at": scored_at,
    }])
    with _fake_db(monkeypatch, conn):
        stats = backfill(dry_run=True, tickets=False)
    assert stats["calls"]["candidates"] == 1
    assert stats["calls"]["written"] == 1
    assert conn.inserts == []

    with _fake_db(monkeypatch, conn):
        stats = backfill(dry_run=False, tickets=False)
    assert stats["calls"]["written"] == 1
    assert stats["calls"]["events"] == 3  # transcription, criterion, scoring
    assert all(kind == "call" for kind, _ in conn.inserts)
    first = conn.inserts[0][1]
    assert first[0] == ORG_A
    assert first[1] == 624
    assert first[2] == "transcription"
    assert json.loads(first[4])["source"] == SOURCE
    last = conn.inserts[-1][1]
    assert last[2] == "scoring"
    assert last[6] > first[6]


def test_backfill_groups_ticket_agents_and_writes_once(monkeypatch):
    scored_at = datetime(2026, 9, 10, tzinfo=timezone.utc)
    conn = _FakeConn(ticket_rows=[
        {
            "org_id": ORG_A,
            "ticket_id": TICKET_A,
            "agent_user_id": "11111111-1111-1111-1111-111111111111",
            "score": 70,
            "findings": {"findings": [{"id": "empathy", "verdict": "pass"}]},
            "scored_at": scored_at,
        },
        {
            "org_id": ORG_A,
            "ticket_id": TICKET_A,
            "agent_user_id": "22222222-2222-2222-2222-222222222222",
            "score": 40,
            "findings": {"findings": [{"id": "empathy", "verdict": "fail"}]},
            "scored_at": scored_at,
        },
    ])
    with _fake_db(monkeypatch, conn):
        stats = backfill(dry_run=False, calls=False)
    assert stats["tickets"]["candidates"] == 1
    assert stats["tickets"]["written"] == 1
    kinds = [k for k, _ in conn.inserts]
    assert kinds[0] == "ticket"
    stages = [p[2] for _, p in conn.inserts]
    assert stages[0] == "parse"
    assert stages[-1] == "scoring"
    assert stages.count("criterion:empathy") == 2


def test_sql_placeholders_are_positional():
    assert _INSERT_CALL.count("%s") == 7
    assert _INSERT_TICKET.count("%s") == 7
    assert "{" not in _INSERT_CALL
    assert "{" not in _INSERT_TICKET
