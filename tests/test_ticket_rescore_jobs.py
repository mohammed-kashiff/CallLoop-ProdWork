"""IN-27 follow-on: internal, platform-admin-only bulk re-score by org +
date range. Runs as a background thread (no queue infra in this
codebase) with progress/ETA tracked in-memory — tests replace the real
thread spawn with a synchronous call so results are deterministic."""

from __future__ import annotations

import uuid

import pytest

from backend import ticket_rescore_jobs as m
from backend.org_ids import DEFAULT_ORG_ID

AGENT_A = "11111111-1111-1111-1111-111111111111"
AGENT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _reset_jobs():
    m.reset_for_tests()
    yield
    m.reset_for_tests()


class _SyncThread:
    """Runs target() immediately in start() instead of a real thread —
    makes the background job deterministic for tests."""

    def __init__(self, target=None, args=(), name=None, daemon=None):
        self._target = target
        self._args = args

    def start(self):
        self._target(*self._args)


def _patch_pipeline(monkeypatch, *, tickets, dimensions=None, fail_ticket_id=None):
    """tickets: {ticket_id: {"messages": [...]}}"""
    monkeypatch.setattr(m.threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        m.ticket_ingest, "list_ready_ticket_ids_in_range",
        lambda org_id, f, t: list(tickets),
    )
    monkeypatch.setattr(
        m.ticket_ingest, "get_ticket",
        lambda tid, org_id: tickets.get(tid),
    )
    monkeypatch.setattr(
        m.ticket_rubric, "ensure_ticket_rubric",
        lambda org_id: {"dimensions": dimensions or [{"id": "d1", "name": "D1", "weight": 100}]},
    )

    def fake_score(turns, dims, *, only_agent_ids=None):
        return [
            {"agent_user_id": a, "score": 80.0, "findings": [], "spans": []}
            for a in (only_agent_ids or [])
        ]

    monkeypatch.setattr(m.ticket_scoring, "score_ticket_per_agent", fake_score)
    upserts = []

    def fake_upsert(ticket_id, org_id, results, *, requested_by=None):
        if ticket_id == fail_ticket_id:
            raise RuntimeError("boom")
        upserts.append((ticket_id, org_id, results, requested_by))
        return [str(uuid.uuid4()) for _ in results]

    monkeypatch.setattr(m.ticket_audit_store, "upsert_many", fake_upsert)
    return upserts


def _turn(seq, speaker, agent_user_id, text="hi"):
    return {
        "seq": seq, "speaker": speaker, "text": text,
        "agent_user_id": agent_user_id, "sent_at": None,
    }


def test_start_backfill_counts_items_from_real_resolved_agents(monkeypatch):
    tickets = {
        "t1": {"messages": [_turn(0, "customer", None), _turn(1, "agent", AGENT_A)]},
        "t2": {"messages": [_turn(0, "agent", AGENT_A), _turn(1, "agent", AGENT_B)]},
    }
    upserts = _patch_pipeline(monkeypatch, tickets=tickets)
    job = m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by="admin-1")
    assert job["total_tickets"] == 2
    assert job["total_items"] == 3  # t1: 1 agent, t2: 2 agents
    assert job["completed_items"] == 3
    assert job["status"] == "done"
    assert job["estimated_seconds_remaining"] == 0
    assert len(upserts) == 2
    assert upserts[0][3] == "admin-1"  # requested_by threaded through


def test_start_backfill_skips_tickets_with_no_resolved_agent(monkeypatch):
    tickets = {
        "t1": {"messages": [_turn(0, "customer", None)]},  # nobody resolved
        "t2": {"messages": [_turn(0, "agent", AGENT_A)]},
    }
    _patch_pipeline(monkeypatch, tickets=tickets)
    job = m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by=None)
    assert job["total_tickets"] == 1
    assert job["total_items"] == 1


def test_start_backfill_with_zero_matching_tickets_is_immediately_done(monkeypatch):
    _patch_pipeline(monkeypatch, tickets={})
    job = m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by=None)
    assert job["status"] == "done"
    assert job["total_items"] == 0
    assert job["finished_at"] is not None


def test_start_backfill_captures_per_ticket_errors_without_failing_the_job(monkeypatch):
    tickets = {
        "t1": {"messages": [_turn(0, "agent", AGENT_A)]},
        "t2": {"messages": [_turn(0, "agent", AGENT_B)]},
    }
    _patch_pipeline(monkeypatch, tickets=tickets, fail_ticket_id="t1")
    job = m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by=None)
    assert job["status"] == "done_with_errors"
    # completed_items tracks progress (attempted, whether it succeeded or
    # not) so the ETA/remaining count is accurate — errors is the
    # separate signal for what actually failed.
    assert job["completed_items"] == job["total_items"] == 2
    assert len(job["errors"]) == 1
    assert job["errors"][0]["ticket_id"] == "t1"


def test_start_backfill_rejects_invalid_org_id(monkeypatch):
    _patch_pipeline(monkeypatch, tickets={})
    with pytest.raises(ValueError, match="org_id"):
        m.start_backfill("not-a-uuid", "2026-09-01", "2026-09-17", requested_by=None)


def test_start_backfill_rejects_malformed_dates(monkeypatch):
    _patch_pipeline(monkeypatch, tickets={})
    with pytest.raises(ValueError, match="from_date"):
        m.start_backfill(DEFAULT_ORG_ID, "not-a-date", "2026-09-17", requested_by=None)


def test_start_backfill_rejects_from_date_after_to_date(monkeypatch):
    _patch_pipeline(monkeypatch, tickets={})
    with pytest.raises(ValueError, match="from_date must not be after"):
        m.start_backfill(DEFAULT_ORG_ID, "2026-09-17", "2026-09-01", requested_by=None)


def test_start_backfill_refuses_a_second_job_while_one_is_running(monkeypatch):
    """Ticket scoring is fully serial — two jobs at once would only slow
    both down, never actually run in parallel."""
    monkeypatch.setattr(m, "_any_job_running", lambda: True)
    with pytest.raises(RuntimeError, match="already running"):
        m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by=None)


def test_get_job_returns_none_for_an_unknown_id():
    assert m.get_job(str(uuid.uuid4())) is None


def test_eta_shrinks_as_items_complete(monkeypatch):
    """Not a fixed guess — recomputed from real elapsed time so far."""
    tickets = {f"t{i}": {"messages": [_turn(0, "agent", AGENT_A)]} for i in range(5)}
    _patch_pipeline(monkeypatch, tickets=tickets)
    job = m.start_backfill(DEFAULT_ORG_ID, "2026-09-01", "2026-09-17", requested_by=None)
    # Synchronous fake runs to completion instantly; ETA is 0 once done.
    assert job["status"] == "done"
    assert job["estimated_seconds_remaining"] == 0
    assert job["completed_items"] == job["total_items"] == 5


# ---------- HTTP layer ----------


def test_start_backfill_route_requires_platform_admin(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    authorize(client, monkeypatch)  # owner, not a platform admin
    r = client.post(
        f"/api/admin/orgs/{DEFAULT_ORG_ID}/ticket-rescore-jobs",
        json={"from_date": "2026-09-01", "to_date": "2026-09-17"},
    )
    assert r.status_code == 403


def test_start_backfill_route_400_on_bad_dates(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    authorize(client, monkeypatch)
    r = client.post(
        f"/api/admin/orgs/{DEFAULT_ORG_ID}/ticket-rescore-jobs",
        json={"from_date": "not-a-date", "to_date": "2026-09-17"},
    )
    assert r.status_code == 400


def test_start_backfill_route_success_shape(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    uid = authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rescore_jobs.start_backfill",
        lambda org_id, from_date, to_date, *, requested_by: {
            "job_id": "job-1", "org_id": org_id, "status": "running",
            "total_tickets": 3, "total_items": 4, "completed_items": 0,
            "estimated_seconds_remaining": 80, "from_date": from_date,
            "to_date": to_date, "started_at": "now", "finished_at": None,
            "errors": [], "requested_by_seen": requested_by,
        },
    )
    r = client.post(
        f"/api/admin/orgs/{DEFAULT_ORG_ID}/ticket-rescore-jobs",
        json={"from_date": "2026-09-01", "to_date": "2026-09-17"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["job_id"] == "job-1"
    assert body["estimated_seconds_remaining"] == 80
    assert body["requested_by_seen"] == uid


def test_start_backfill_route_409_when_already_running(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    authorize(client, monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("A ticket rescore backfill is already running.")

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rescore_jobs.start_backfill", boom,
    )
    r = client.post(
        f"/api/admin/orgs/{DEFAULT_ORG_ID}/ticket-rescore-jobs",
        json={"from_date": "2026-09-01", "to_date": "2026-09-17"},
    )
    assert r.status_code == 409


def test_get_backfill_job_route_requires_platform_admin(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ticket-rescore-jobs/some-job-id")
    assert r.status_code == 403


def test_get_backfill_job_route_404_for_unknown_job(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    authorize(client, monkeypatch)
    r = client.get("/api/admin/ticket-rescore-jobs/some-job-id")
    assert r.status_code == 404


def test_get_backfill_job_route_returns_the_job(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rescore_jobs.get_job",
        lambda job_id: {"job_id": job_id, "status": "running"} if job_id == "real-job" else None,
    )
    r = client.get("/api/admin/ticket-rescore-jobs/real-job")
    assert r.status_code == 200
    assert r.json()["job_id"] == "real-job"
