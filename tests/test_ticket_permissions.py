"""TA-12: manager full-thread view vs. agent own-contribution view.

No new mechanism (PRD §7) — org_members.role's existing "owner" stands in
for "manager" (same substitution auth.is_org_owner() already makes for
require_owner()). Pure filtering logic here; tests/test_ticket_api.py and
tests/test_ticket_score_api.py cover the HTTP-layer wiring."""

from __future__ import annotations

import uuid

from backend import ticket_permissions as perm
from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import mint_access_token

AGENT_A = "11111111-1111-1111-1111-111111111111"
AGENT_B = "22222222-2222-2222-2222-222222222222"

TURNS = [
    {"seq": 0, "speaker": "customer", "agent_user_id": None, "text": "hi"},
    {"seq": 1, "speaker": "agent", "agent_user_id": AGENT_A, "text": "I'll look into it"},
    {"seq": 2, "speaker": "customer", "agent_user_id": None, "text": "still broken"},
    {"seq": 3, "speaker": "agent", "agent_user_id": AGENT_B, "text": "fixed and redeployed"},
    {"seq": 4, "speaker": "customer", "agent_user_id": None, "text": "thanks!"},
]

FINDINGS = [
    {"id": "ack", "verdict": "pass", "attributed_to": AGENT_A},
    {"id": "fixed", "verdict": "pass", "attributed_to": AGENT_B},
    {"id": "unattributed", "verdict": "not_applicable", "attributed_to": None},
]

SPANS = [
    {"agent_user_id": AGENT_A, "start_seq": 1, "end_seq": 2, "turn_count": 1},
    {"agent_user_id": AGENT_B, "start_seq": 3, "end_seq": 4, "turn_count": 1},
]


# ---------- filter_turns_for_viewer ----------


def test_manager_sees_every_turn_unchanged():
    out = perm.filter_turns_for_viewer(TURNS, viewer_user_id=AGENT_A, is_manager=True)
    assert out == TURNS


def test_agent_sees_only_turns_inside_their_own_span():
    out = perm.filter_turns_for_viewer(TURNS, viewer_user_id=AGENT_A, is_manager=False)
    assert [t["seq"] for t in out] == [1, 2]


def test_agent_never_sees_another_agents_span():
    out = perm.filter_turns_for_viewer(TURNS, viewer_user_id=AGENT_A, is_manager=False)
    assert 3 not in {t["seq"] for t in out}
    assert 4 not in {t["seq"] for t in out}


def test_pre_first_agent_turns_are_excluded_for_a_non_manager():
    """seq 0 (customer, before any agent spoke) belongs to no span."""
    out = perm.filter_turns_for_viewer(TURNS, viewer_user_id=AGENT_A, is_manager=False)
    assert 0 not in {t["seq"] for t in out}


def test_agent_with_no_turns_on_this_ticket_sees_nothing():
    stranger = "33333333-3333-3333-3333-333333333333"
    out = perm.filter_turns_for_viewer(TURNS, viewer_user_id=stranger, is_manager=False)
    assert out == []


# ---------- filter_findings_for_viewer ----------


def test_manager_sees_every_finding_unchanged():
    out = perm.filter_findings_for_viewer(FINDINGS, viewer_user_id=AGENT_A, is_manager=True)
    assert out == FINDINGS


def test_agent_sees_only_their_own_attributed_findings():
    out = perm.filter_findings_for_viewer(FINDINGS, viewer_user_id=AGENT_A, is_manager=False)
    assert [f["id"] for f in out] == ["ack"]


def test_agent_never_sees_another_agents_finding():
    out = perm.filter_findings_for_viewer(FINDINGS, viewer_user_id=AGENT_A, is_manager=False)
    assert all(f["id"] != "fixed" for f in out)


def test_unattributed_findings_are_excluded_for_a_non_manager():
    out = perm.filter_findings_for_viewer(FINDINGS, viewer_user_id=AGENT_A, is_manager=False)
    assert all(f["id"] != "unattributed" for f in out)


# ---------- filter_spans_for_viewer ----------


def test_manager_sees_every_span_unchanged():
    out = perm.filter_spans_for_viewer(SPANS, viewer_user_id=AGENT_A, is_manager=True)
    assert out == SPANS


def test_agent_sees_only_their_own_span():
    out = perm.filter_spans_for_viewer(SPANS, viewer_user_id=AGENT_B, is_manager=False)
    assert len(out) == 1
    assert out[0]["agent_user_id"] == AGENT_B


def test_module_never_touches_the_database():
    """Pure filtering only — by design it never needs db.connection() or
    org_scope(), so it can't bypass RLS even by accident."""
    from backend.paths import ROOT

    src = (ROOT / "backend" / "ticket_permissions.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src
    assert "db.connection" not in src
    assert "org_scope" not in src


def test_module_does_not_import_the_call_scoring_engine():
    import ast

    from backend.paths import ROOT

    src = (ROOT / "backend" / "ticket_permissions.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert "qa_engine" not in mod
            assert "qa_v8" not in mod
            assert "rules_v8" not in mod


# ---------- HTTP layer: GET /api/tickets/{id}, POST .../score, GET .../mine ----------


def _authorize_as(client, monkeypatch, *, role: str, org_id: str | None = None, sub: str | None = None) -> str:
    """Same shape as tests.conftest.authorize(), but lets the role vary —
    that helper hardcodes "owner", which is exactly what TA-12 needs to
    vary to exercise the manager/agent split."""
    from backend.auth import Membership

    uid = sub or str(uuid.uuid4())
    tenant = org_id or DEFAULT_ORG_ID
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            tenant, role, str(user_id)
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    return uid


def _fake_ticket_row(agent_a: str, agent_b: str) -> dict:
    return {
        "id": "t1", "source": "pdf_upload", "status": "ready", "created_at": None,
        "messages": [
            {"seq": 0, "speaker": "customer", "text": "hi", "agent_user_id": None,
             "sent_at": None, "has_image": False},
            {"seq": 1, "speaker": "agent", "text": "on it", "agent_user_id": agent_a,
             "sent_at": None, "has_image": False},
            {"seq": 2, "speaker": "agent", "text": "fixed", "agent_user_id": agent_b,
             "sent_at": None, "has_image": True},
        ],
        "assets": [{"seq": 2, "width": 10, "height": 10, "content_type": "image/png"}],
        "audit": {
            "score": 100.0, "created_at": None, "primary_owner": agent_b,
            "spans": [
                {"agent_user_id": agent_a, "start_seq": 1, "end_seq": 1, "turn_count": 1},
                {"agent_user_id": agent_b, "start_seq": 2, "end_seq": 2, "turn_count": 1},
            ],
            "findings": [
                {"id": "a", "verdict": "pass", "attributed_to": agent_a},
                {"id": "b", "verdict": "pass", "attributed_to": agent_b},
            ],
        },
    }


def test_get_ticket_as_manager_returns_full_scope(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket_row(agent_a, agent_b),
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="owner")
    r = client.get(f"/api/tickets/{uuid.uuid4()}")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "full"
    assert len(body["messages"]) == 3
    assert len(body["audit"]["findings"]) == 2


def test_get_ticket_as_agent_returns_only_their_own_contribution(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket_row(agent_a, agent_b),
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="member", sub=agent_a)
    r = client.get(f"/api/tickets/{uuid.uuid4()}")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "own"
    assert [m["seq"] for m in body["messages"]] == [1]
    assert body["assets"] == []  # the only asset is on agent_b's turn (seq 2)
    assert [f["id"] for f in body["audit"]["findings"]] == ["a"]
    assert len(body["audit"]["spans"]) == 1


def test_score_route_filters_findings_for_a_non_manager(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket_row = _fake_ticket_row(agent_a, agent_b)
    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: ticket_row)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.fetch_latest", lambda *a, **k: None)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.upsert", lambda *a, **k: "audit-id")
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"id": "rubric-id", "name": "Ticket QA", "version": 1, "dimensions": []},
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_scoring.score_ticket",
        lambda turns, dims, **k: {
            "score": 100.0, "primary_owner": agent_b,
            "spans": [
                {"agent_user_id": agent_a, "start_seq": 1, "end_seq": 1, "turn_count": 1},
                {"agent_user_id": agent_b, "start_seq": 2, "end_seq": 2, "turn_count": 1},
            ],
            "findings": [
                {"id": "a", "verdict": "pass", "attributed_to": agent_a},
                {"id": "b", "verdict": "pass", "attributed_to": agent_b},
            ],
        },
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="member", sub=agent_a)
    r = client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "own"
    # "a" is agent_a's own finding; response_timeliness is always appended
    # and always visible (a whole-thread metric, not one agent's score —
    # never TA-12-filtered). "b" (agent_b's) must never appear.
    assert [f["id"] for f in body["findings"]] == ["a", "response_timeliness"]
    assert len(body["spans"]) == 1


def test_my_ticket_contributions_rolls_up_only_the_callers_own_turns(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket_row = _fake_ticket_row(agent_a, agent_b)
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.list_ticket_ids_for_agent",
        lambda org_id, uid: ["t1"],
    )
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.get_ticket", lambda *a, **k: ticket_row,
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="member", sub=agent_a)
    r = client.get("/api/tickets/mine")
    assert r.status_code == 200
    body = r.json()
    assert len(body["tickets"]) == 1
    entry = body["tickets"][0]
    assert entry["ticket_id"] == "t1"
    assert [t["seq"] for t in entry["turns"]] == [1]
    assert [f["id"] for f in entry["findings"]] == ["a"]


def test_my_ticket_contributions_401_without_token():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/tickets/mine")
    assert r.status_code == 401
