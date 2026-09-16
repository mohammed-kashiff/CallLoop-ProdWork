"""TA-12, rebuilt TA-21/TA-29/TA-30: manager full-scorecard view vs. agent
own-scorecard view — but the full thread, always, for every viewer.

TA-30 deleted filter_turns_for_viewer/filter_spans_for_viewer entirely: the
bug found live was that a non-manager's thread got stripped down to only
their own span, so an agent reviewing their own ticket couldn't see what the
customer originally asked. own_span_seqs replaces that — it tells the caller
which turns to *highlight* as the viewer's own in the always-full thread,
never which turns to hide. filter_audits_for_viewer is the only remaining
access control: which agent's independent scorecard(s) (TA-28) a viewer
gets. Pure filtering logic here; tests/test_ticket_api.py and
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

AUDITS = [
    {"agent_user_id": AGENT_A, "score": 100.0, "findings": [{"id": "ack", "verdict": "pass"}]},
    {"agent_user_id": AGENT_B, "score": 80.0, "findings": [{"id": "fixed", "verdict": "pass"}]},
]


# ---------- own_span_seqs ----------


def test_own_span_seqs_includes_customer_turns_inside_the_viewers_span():
    """seq 2 (customer, "still broken") sits inside agent_a's span (1-2)
    since a span runs until a *different* agent speaks — it's part of what
    agent_a is responsible for, so it's part of their own highlight set."""
    out = perm.own_span_seqs(TURNS, AGENT_A)
    assert out == [1, 2]


def test_own_span_seqs_never_includes_another_agents_span():
    out = perm.own_span_seqs(TURNS, AGENT_A)
    assert 3 not in out
    assert 4 not in out


def test_own_span_seqs_excludes_pre_first_agent_turns():
    """seq 0 (customer, before any agent spoke) belongs to no span."""
    out = perm.own_span_seqs(TURNS, AGENT_A)
    assert 0 not in out


def test_own_span_seqs_for_the_other_agent():
    out = perm.own_span_seqs(TURNS, AGENT_B)
    assert out == [3, 4]


def test_own_span_seqs_empty_for_a_stranger_with_no_turns_on_this_ticket():
    stranger = "33333333-3333-3333-3333-333333333333"
    out = perm.own_span_seqs(TURNS, stranger)
    assert out == []


# ---------- filter_audits_for_viewer ----------


def test_manager_sees_every_agents_audit_unchanged():
    out = perm.filter_audits_for_viewer(AUDITS, viewer_user_id=AGENT_A, is_manager=True)
    assert out == AUDITS


def test_non_manager_sees_only_their_own_audit():
    out = perm.filter_audits_for_viewer(AUDITS, viewer_user_id=AGENT_A, is_manager=False)
    assert len(out) == 1
    assert out[0]["agent_user_id"] == AGENT_A


def test_non_manager_never_sees_a_teammates_audit():
    out = perm.filter_audits_for_viewer(AUDITS, viewer_user_id=AGENT_A, is_manager=False)
    assert all(a["agent_user_id"] != AGENT_B for a in out)


def test_non_manager_with_no_stored_audit_sees_nothing():
    stranger = "33333333-3333-3333-3333-333333333333"
    out = perm.filter_audits_for_viewer(AUDITS, viewer_user_id=stranger, is_manager=False)
    assert out == []


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


def test_thread_filtering_helpers_no_longer_exist():
    """TA-30: filter_turns_for_viewer/filter_spans_for_viewer were deleted
    outright, not deprecated — the whole thread is never filtered by
    viewer again, so nothing should still call the old names."""
    assert not hasattr(perm, "filter_turns_for_viewer")
    assert not hasattr(perm, "filter_spans_for_viewer")
    assert not hasattr(perm, "filter_findings_for_viewer")


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
        "audits": [
            {
                "agent_user_id": agent_a, "score": 100.0,
                "created_at": None, "updated_at": None,
                "findings": [{"id": "a", "verdict": "pass"}],
                "spans": [{"agent_user_id": agent_a, "start_seq": 1, "end_seq": 1, "turn_count": 1}],
            },
            {
                "agent_user_id": agent_b, "score": 90.0,
                "created_at": None, "updated_at": None,
                "findings": [{"id": "b", "verdict": "pass"}],
                "spans": [{"agent_user_id": agent_b, "start_seq": 2, "end_seq": 2, "turn_count": 1}],
            },
        ],
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
    assert len(body["audits"]) == 2


def test_get_ticket_as_manager_role_also_returns_full_scope(monkeypatch):
    """AC-56/AC-60: the TA-12 gate widened from owner-only to owner-or-
    manager — a Manager must get the same full-scorecard view an Owner
    does."""
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket_row(agent_a, agent_b),
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="manager")
    r = client.get(f"/api/tickets/{uuid.uuid4()}")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "full"
    assert len(body["messages"]) == 3
    assert len(body["audits"]) == 2


def test_get_ticket_as_agent_gets_the_full_thread_but_only_their_own_scorecard(monkeypatch):
    """TA-30: unlike the old behavior, a non-manager still gets every
    message and asset — only which agent's scorecard(s) they see is
    scoped. own_span_seqs tells the frontend which turns are theirs to
    highlight in that full thread."""
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
    assert [m["seq"] for m in body["messages"]] == [0, 1, 2]
    assert len(body["assets"]) == 1  # the full thread's asset, not filtered out
    assert [a["agent_user_id"] for a in body["audits"]] == [agent_a]
    assert body["own_span_seqs"] == [1]


def test_score_route_returns_every_agents_scorecard_for_a_manager(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket_row = _fake_ticket_row(agent_a, agent_b)
    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: ticket_row)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many", lambda *a, **k: ["id-a", "id-b"],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"id": "rubric-id", "name": "Ticket QA", "version": 1, "dimensions": []},
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_scoring.score_ticket_per_agent",
        lambda turns, dims, *, only_agent_ids=None, **k: [
            {"agent_user_id": aid, "score": 100.0, "findings": [{"id": "x", "verdict": "pass"}], "spans": []}
            for aid in only_agent_ids
        ],
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="owner")
    r = client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "full"
    assert {a["agent_user_id"] for a in body["agents"]} == {agent_a, agent_b}


def test_score_route_filters_agents_for_a_non_manager(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    agent_a, agent_b = str(uuid.uuid4()), str(uuid.uuid4())
    ticket_row = _fake_ticket_row(agent_a, agent_b)
    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: ticket_row)
    monkeypatch.setattr("backend.ticket_score_api.ticket_audit_store.fetch_all", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many", lambda *a, **k: ["id-a", "id-b"],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"id": "rubric-id", "name": "Ticket QA", "version": 1, "dimensions": []},
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_scoring.score_ticket_per_agent",
        lambda turns, dims, *, only_agent_ids=None, **k: [
            {
                "agent_user_id": aid, "score": 100.0,
                "findings": [{"id": "a" if aid == agent_a else "b", "verdict": "pass"}],
                "spans": [],
            }
            for aid in only_agent_ids
        ],
    )
    client = TestClient(app)
    _authorize_as(client, monkeypatch, role="member", sub=agent_a)
    r = client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "own"
    # agent_a's own scorecard only — agent_b's must never appear.
    assert len(body["agents"]) == 1
    agent_result = body["agents"][0]
    assert agent_result["agent_user_id"] == agent_a
    # "a" is agent_a's own finding; response_timeliness is always appended
    # per agent (a deterministic metric, never TA-12-filtered).
    assert [f["id"] for f in agent_result["findings"]] == ["a", "response_timeliness"]


def test_my_ticket_contributions_rolls_up_the_full_thread_but_only_own_findings(monkeypatch):
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
    # TA-30: the full thread, not just the caller's own turns.
    assert [t["seq"] for t in entry["turns"]] == [0, 1, 2]
    assert entry["own_span_seqs"] == [1]
    assert entry["findings"] == [{"id": "a", "verdict": "pass"}]


def test_my_ticket_contributions_401_without_token():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/tickets/mine")
    assert r.status_code == 401
