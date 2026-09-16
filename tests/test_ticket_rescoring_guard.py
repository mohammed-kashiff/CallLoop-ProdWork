"""TA-11, rebuilt per-agent (TA-28): an already-audited AGENT does not
silently re-score — but the guard is per agent, not per ticket.

Mirrors the call-side enable_call_rescoring guard. Off by default. An
agent never before scored on this ticket is always allowed a first score,
even if a teammate on the same ticket already has a stored row. A later
POST for an agent who already has a row returns their stored scorecard (no
Claude) unless ?refresh=true, which is 403 unless enable_ticket_rescoring
is on — and then re-scores every resolved agent, not just the ones with
existing rows.
"""

from __future__ import annotations

import uuid

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.test_ticket_score_api import AGENT_ID, _fake_ticket

AGENT_B = "55555555-5555-5555-5555-555555555555"

STORED_A = {
    "id": "audit-1", "agent_user_id": AGENT_ID, "score": 77,
    "findings": [{"id": "tone", "verdict": "pass"}], "spans": [],
    "requested_by": None, "created_at": "2026-09-05T00:00:00+00:00",
    "updated_at": "2026-09-05T00:00:00+00:00",
}


def _fresh_score(turns, dims, *, only_agent_ids=None, **_k):
    return [
        {
            "agent_user_id": aid, "score": 90, "spans": [],
            "findings": [{"id": "tone", "verdict": "fail"}],
        }
        for aid in only_agent_ids
    ]


def test_flag_defaults_off():
    from backend.org_features import DEFAULT_OFF_KEYS, default_features

    assert "enable_ticket_rescoring" in DEFAULT_OFF_KEYS
    assert default_features()["enable_ticket_rescoring"] is False


def test_first_score_always_allowed_when_flag_off(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": False},
    )
    wrote = {}

    def fake_upsert_many(ticket_id, org_id, agent_results, *, requested_by=None):
        wrote["agent_results"] = agent_results
        wrote["org_id"] = org_id
        return ["new-audit"]

    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.upsert_many", fake_upsert_many)
    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _fresh_score)

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["agents"][0]["score"] == 90
    assert wrote["agent_results"][0]["agent_user_id"] == AGENT_ID
    assert wrote["org_id"] == DEFAULT_ORG_ID


def test_second_post_returns_stored_score_and_does_not_call_claude(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [STORED_A],
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not re-run Claude on an already-audited agent")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _boom)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.upsert_many", _boom)

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is True
    assert body["agents"][0]["score"] == 77


def test_a_newly_resolved_agent_still_gets_a_first_score_when_a_teammate_is_already_audited(
    auth_client, monkeypatch,
):
    """TA-28's whole point: the guard is per-agent. Agent A already has a
    stored row; Agent B (newly resolved on the same ticket) must still
    get a real first score on a plain POST — this is not a re-score of
    already-audited data, since B has never been scored."""
    two_agent_ticket = _fake_ticket(messages=[
        {"seq": 0, "speaker": "customer", "text": "hi",
         "agent_user_id": None, "sent_at": None, "has_image": False},
        {"seq": 1, "speaker": "agent", "text": "on it",
         "agent_user_id": AGENT_ID, "sent_at": None, "has_image": False},
        {"seq": 2, "speaker": "agent", "text": "fixed",
         "agent_user_id": AGENT_B, "sent_at": None, "has_image": False},
    ])
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: two_agent_ticket,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [STORED_A],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"id": "rubric-id", "name": "Ticket QA", "version": 1, "dimensions": []},
    )

    scored_ids = []

    def fake_score(turns, dims, *, only_agent_ids=None, **_k):
        scored_ids.extend(only_agent_ids)
        return _fresh_score(turns, dims, only_agent_ids=only_agent_ids)

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", fake_score)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many", lambda *a, **k: ["new-audit"],
    )

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    # Only B was newly scored — A's already-stored row was never re-run.
    assert scored_ids == [AGENT_B]
    assert not body["cached"]
    by_agent = {a["agent_user_id"]: a for a in body["agents"]}
    assert by_agent[AGENT_ID]["score"] == 77  # A's stored score, untouched
    assert by_agent[AGENT_B]["score"] == 90  # B's fresh score


def test_refresh_blocked_when_flag_off(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [STORED_A],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": False},
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not re-run Claude when rescoring is disabled")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _boom)

    r = auth_client.post(
        f"/api/tickets/{uuid.uuid4()}/score", params={"refresh": "true"},
    )
    assert r.status_code == 403
    assert "already been audited" in r.json()["detail"]


def test_refresh_allowed_when_flag_on(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [STORED_A],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": True},
    )
    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _fresh_score)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many",
        lambda *a, **k: ["audit-id"],
    )

    r = auth_client.post(
        f"/api/tickets/{uuid.uuid4()}/score", params={"refresh": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cached"] is False
    assert body["agents"][0]["score"] == 90


def test_ticket_score_api_does_not_import_call_audit_store():
    src = (ROOT / "backend" / "ticket_score_api.py").read_text(encoding="utf-8")
    assert "from . import audit_store" not in src
    assert "from .audit_store" not in src
    assert "features_for_org(org_id).get(\"enable_ticket_rescoring\")" in src
    assert "features_for_org(org_id).get(\"enable_call_rescoring\")" not in src


def test_call_engine_files_untouched():
    for name in ("qa_engine.py", "qa_v8.py", "rules_v8.py"):
        src = (ROOT / "backend" / name).read_text(encoding="utf-8")
        assert "ticket_audit_store" not in src
        assert "enable_ticket_rescoring" not in src
