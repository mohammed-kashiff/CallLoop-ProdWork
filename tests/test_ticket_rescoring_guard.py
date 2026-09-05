"""TA-11: an already-audited ticket does not silently re-score.

Mirrors the call-side enable_call_rescoring guard. Off by default.
A never-before-audited ticket is always allowed to score. A later
POST without ?refresh=true returns the stored scorecard (no Claude).
?refresh=true is 403 unless enable_ticket_rescoring is on.
"""

from __future__ import annotations

import uuid

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.test_ticket_score_api import _fake_ticket

STORED = {
    "id": "audit-1",
    "score": 77,
    "findings": {
        "score": 77,
        "primary_owner": None,
        "spans": [],
        "findings": [{"id": "tone", "verdict": "pass"}],
    },
    "requested_by": None,
    "created_at": "2026-09-05T00:00:00+00:00",
}


def _fresh_score(*_a, **_k):
    return {
        "score": 90,
        "primary_owner": None,
        "spans": [],
        "findings": [{"id": "tone", "verdict": "fail"}],
    }


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
        "backend.ticket_score_api.ticket_audit_store.fetch_latest",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": False},
    )
    wrote = {}

    def fake_upsert(ticket_id, org_id, findings, *, requested_by=None):
        wrote["findings"] = findings
        wrote["org_id"] = org_id
        return "new-audit"

    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.upsert", fake_upsert)
    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", _fresh_score)

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    assert r.json()["cached"] is False
    assert r.json()["score"] == 90
    assert wrote["findings"]["score"] == 90
    assert wrote["org_id"] == DEFAULT_ORG_ID


def test_second_post_returns_stored_score_and_does_not_call_claude(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_latest",
        lambda *a, **k: STORED,
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not re-run Claude on an already-audited ticket")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", _boom)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.upsert", _boom)

    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    assert r.json()["cached"] is True
    assert r.json()["score"] == 77


def test_refresh_blocked_when_flag_off(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_latest",
        lambda *a, **k: STORED,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": False},
    )

    def _boom(*_a, **_k):
        raise AssertionError("must not re-run Claude when rescoring is disabled")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", _boom)

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
        "backend.ticket_score_api.ticket_audit_store.fetch_latest",
        lambda *a, **k: STORED,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.org_features.features_for_org",
        lambda org_id: {"enable_ticket_rescoring": True},
    )
    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", _fresh_score)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert",
        lambda *a, **k: "audit-id",
    )

    r = auth_client.post(
        f"/api/tickets/{uuid.uuid4()}/score", params={"refresh": "true"},
    )
    assert r.status_code == 200
    assert r.json()["cached"] is False
    assert r.json()["score"] == 90


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
