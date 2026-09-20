"""IN-30/IN-33: Auto Audit for Intercom Tickets.

auto_audit_ticket() is the Intercom ingestion path's own trigger for
scoring — reuses the exact same core score_ticket_route already uses
(IN-32's _score_ticket_core), just with triggered_by="auto" and no
viewer. It must never raise: any reason scoring didn't happen (flag
off, no resolved agents, rate-limited, a real failure) is swallowed so
ingestion — which has already fully succeeded by the time this runs —
is never affected.
"""

from __future__ import annotations

from fastapi import HTTPException

from backend import ticket_score_api
from tests.test_ticket_score_api import AGENT_ID, _fake_ticket


def _fresh_score(turns, dims, *, only_agent_ids=None, **_k):
    return [
        {"agent_user_id": aid, "score": 90, "spans": [], "findings": [{"id": "tone", "verdict": "pass"}]}
        for aid in only_agent_ids
    ]


def test_auto_audit_does_nothing_when_flag_is_off(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_auto_audit": False},
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not touch ticket_ingest at all when the flag is off")

    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", _boom)
    ticket_score_api.auto_audit_ticket("org-1", "ticket-1")  # must not raise


def test_auto_audit_scores_and_persists_as_triggered_by_auto(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_auto_audit": True, "enable_ticket_rescoring": False},
    )
    monkeypatch.setattr("backend.ticket_score_api.ticket_trail.record", lambda *a, **k: None)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _fresh_score,
    )
    written: dict = {}

    def fake_upsert_many(ticket_id, org_id, agent_results, *, requested_by=None, triggered_by="manual"):
        written["agent_results"] = agent_results
        written["requested_by"] = requested_by
        written["triggered_by"] = triggered_by
        return ["audit-id"]

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many", fake_upsert_many,
    )
    from backend.ticket_rubric import get_default_ticket_rubric

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"dimensions": get_default_ticket_rubric()},
    )

    ticket_score_api.auto_audit_ticket("org-1", "ticket-1")

    assert written["triggered_by"] == "auto"
    assert written["requested_by"] is None  # no human requester for an auto-triggered score
    assert written["agent_results"][0]["agent_user_id"] == AGENT_ID
    assert written["agent_results"][0]["triggered_by"] == "auto"


def test_auto_audit_swallows_no_resolved_agents_without_raising(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_auto_audit": True},
    )
    monkeypatch.setattr("backend.ticket_score_api.ticket_trail.record", lambda *a, **k: None)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(messages=[
            {"seq": 0, "speaker": "customer", "text": "hi",
             "agent_user_id": None, "sent_at": None, "has_image": False},
        ]),
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not call Claude when no agent identities are resolved")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _boom)

    ticket_score_api.auto_audit_ticket("org-1", "ticket-1")  # must not raise


def test_auto_audit_swallows_rate_limit_without_raising(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_auto_audit": True},
    )

    def _rate_limited(*_a, **_k):
        raise HTTPException(status_code=429, detail="Too many requests.")

    monkeypatch.setattr("backend.ticket_score_api.rate_limit.enforce", _rate_limited)

    def _boom(*_a, **_k):
        raise AssertionError("must never reach ticket lookup once rate-limited")

    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", _boom)
    ticket_score_api.auto_audit_ticket("org-1", "ticket-1")  # must not raise


def test_auto_audit_swallows_and_reports_an_unexpected_exception(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_auto_audit": True},
    )

    def _boom(*_a, **_k):
        raise RuntimeError("db is down")

    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", _boom)
    reported: list = []
    monkeypatch.setattr(
        "backend.ticket_score_api.sentry_report.capture_exception",
        lambda e: reported.append(e),
    )

    ticket_score_api.auto_audit_ticket("org-1", "ticket-1")  # must not raise
    assert len(reported) == 1


def test_manual_score_route_still_writes_triggered_by_manual(auth_client, monkeypatch):
    """score_ticket_route (the HTTP path) must keep behaving exactly as
    before this refactor — same triggered_by='manual' provenance on
    every row it writes."""
    monkeypatch.setattr("backend.ticket_score_api.ticket_trail.record", lambda *a, **k: None)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _fresh_score,
    )
    written: dict = {}

    def fake_upsert_many(ticket_id, org_id, agent_results, *, requested_by=None, triggered_by="manual"):
        written["triggered_by"] = triggered_by
        return ["audit-id"]

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many", fake_upsert_many,
    )
    from backend.ticket_rubric import get_default_ticket_rubric

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"dimensions": get_default_ticket_rubric()},
    )

    import uuid

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    assert written["triggered_by"] == "manual"
    assert r.json()["agents"][0]["triggered_by"] == "manual"
