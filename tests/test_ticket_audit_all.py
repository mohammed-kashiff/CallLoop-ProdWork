"""POST /api/tickets/audit-all — the Audits page's "Audit all" button.
Scores every ready, not-yet-audited ticket in one request by calling
score_ticket_route() per ticket (same rate limit, same per-agent
skip-if-already-scored logic, same HTTPException shapes) rather than
duplicating its scoring/persistence logic."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.api import app
from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

AGENT_ID = "44444444-4444-4444-4444-444444444444"


def _ticket(tid: str, *, status: str = "ready", has_audit: bool = False) -> dict:
    return {"id": tid, "source": "intercom_api", "status": status, "has_audit": has_audit}


def test_audit_all_401_without_token():
    client = TestClient(app)
    r = client.post("/api/tickets/audit-all")
    assert r.status_code == 401


def test_audit_all_requires_owner_or_manager(monkeypatch):
    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_ID)
    from backend.auth import Membership

    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    r = client.post("/api/tickets/audit-all")
    assert r.status_code == 403


def test_audit_all_only_scores_ready_unaudited_tickets(auth_client, monkeypatch):
    tickets = [
        _ticket("t1"),
        _ticket("t2", has_audit=True),  # already scored — skipped
        _ticket("t3", status="processing"),  # not ready yet — skipped
        _ticket("t4"),
    ]
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.list_tickets", lambda org_id: tickets,
    )
    scored_ids = []

    def fake_score(request, ticket_id, refresh=False):
        scored_ids.append(ticket_id)
        return {"agents": []}

    monkeypatch.setattr("backend.ticket_score_api.score_ticket_route", fake_score)
    r = auth_client.post("/api/tickets/audit-all")
    assert r.status_code == 200
    body = r.json()
    assert sorted(scored_ids) == ["t1", "t4"]
    assert body == {"candidates": 2, "attempted": 2, "scored": 2, "errors": [], "remaining": 0}


def test_audit_all_reports_per_ticket_errors_without_failing_the_batch(auth_client, monkeypatch):
    tickets = [_ticket("t1"), _ticket("t2")]
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.list_tickets", lambda org_id: tickets,
    )

    def fake_score(request, ticket_id, refresh=False):
        if ticket_id == "t2":
            raise HTTPException(
                status_code=400,
                detail="No agent identities are resolved on this ticket yet — map agent names first.",
            )
        return {"agents": []}

    monkeypatch.setattr("backend.ticket_score_api.score_ticket_route", fake_score)
    r = auth_client.post("/api/tickets/audit-all")
    assert r.status_code == 200
    body = r.json()
    assert body["scored"] == 1
    assert len(body["errors"]) == 1
    assert body["errors"][0]["ticket_id"] == "t2"
    assert body["errors"][0]["status_code"] == 400


def test_audit_all_caps_the_batch_and_reports_remaining(auth_client, monkeypatch):
    from backend import ticket_score_api

    tickets = [_ticket(f"t{i}") for i in range(ticket_score_api._BULK_AUDIT_MAX_PER_CALL + 5)]
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.list_tickets", lambda org_id: tickets,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.score_ticket_route",
        lambda request, ticket_id, refresh=False: {"agents": []},
    )
    r = auth_client.post("/api/tickets/audit-all")
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == ticket_score_api._BULK_AUDIT_MAX_PER_CALL + 5
    assert body["attempted"] == ticket_score_api._BULK_AUDIT_MAX_PER_CALL
    assert body["scored"] == ticket_score_api._BULK_AUDIT_MAX_PER_CALL
    assert body["remaining"] == 5


def test_audit_all_is_a_noop_with_zero_candidates(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.list_tickets", lambda org_id: [],
    )
    r = auth_client.post("/api/tickets/audit-all")
    assert r.status_code == 200
    assert r.json() == {"candidates": 0, "attempted": 0, "scored": 0, "errors": [], "remaining": 0}


def test_audit_all_route_is_not_swallowed_by_ticket_id_score_route(auth_client, monkeypatch):
    """Regression guard, same concern the ticket_rubric route test guards
    for: /api/tickets/audit-all must not be treated as
    /api/tickets/{ticket_id}/score with ticket_id='audit-all'."""
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.list_tickets", lambda org_id: [],
    )
    r = auth_client.post("/api/tickets/audit-all")
    assert r.status_code == 200
    assert "candidates" in r.json()
