"""POST /api/tickets/{ticket_id}/score — wires TA-6 (ticket_scoring.py) +
TA-7 (ticket_rubric.py, scaffold) to an HTTP route. Neither TA-9's upload
route nor any other route did this; ticket_score_api.py fills that gap
so TA-10 has a real endpoint to render.

Rebuilt for TA-21/TA-25/TA-28: scoring is per-agent now, not per-ticket —
every resolved agent gets their own independent scorecard in the response's
"agents" list, and the rescoring guard/viewer filtering operate per agent,
not on the ticket as a whole."""

from __future__ import annotations

import ast
import uuid

import pytest

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT

CALL_ENGINE = ("qa_engine", "qa_v8", "rules_v8", "transcribe")

AGENT_ID = "44444444-4444-4444-4444-444444444444"


def test_does_not_import_the_call_engine_directly():
    """ticket_scoring.py itself is allowed (and required) here — this
    route's whole job is calling it. It must not reach past that into
    qa_engine/qa_v8/rules_v8/transcribe directly."""
    src = (ROOT / "backend" / "ticket_score_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in CALL_ENGINE, alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            bits = {p for p in mod.split(".") if p}
            assert not (bits & set(CALL_ENGINE)), mod


def test_never_bypasses_rls():
    src = (ROOT / "backend" / "ticket_score_api.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src
    assert "org_id_from_request" in src


def test_uses_the_orgs_ticket_qa_rubric_not_a_hardcoded_constant():
    """TA-13: scoring reads the org's real rubrics-table row (seeded on
    first use), not just the in-memory scaffold constant directly."""
    src = (ROOT / "backend" / "ticket_score_api.py").read_text(encoding="utf-8")
    assert "ticket_rubric" in src
    assert "ensure_ticket_rubric" in src


def test_score_401_without_token():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 401


def test_score_rejects_non_uuid(auth_client):
    r = auth_client.post("/api/tickets/not-a-uuid/score")
    assert r.status_code == 400


def test_score_404_when_ticket_missing(auth_client, monkeypatch):
    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", lambda *a, **k: None)
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 404


def _fake_ticket(status="ready", messages=None):
    return {
        "id": "irrelevant", "source": "pdf_upload", "status": status,
        "created_at": "2026-09-05T00:00:00+00:00",
        "messages": messages if messages is not None else [
            {"seq": 0, "speaker": "customer", "text": "504 on checkout",
             "agent_user_id": None, "sent_at": None, "has_image": False},
            {"seq": 1, "speaker": "agent", "text": "Fixed the payment worker.",
             "agent_user_id": AGENT_ID, "sent_at": None, "has_image": False},
        ],
        "assets": [],
        "audits": [],
    }


def _no_prior_audit(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_all",
        lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert_many",
        lambda *a, **k: ["audit-id"],
    )
    # TA-13: score_ticket_route calls ensure_ticket_rubric() before
    # scoring — without this mock these "mocked" tests would silently
    # hit the real rubrics table for DEFAULT_ORG_ID on every run. Reuses
    # the real default content (pure, no DB) rather than an empty list,
    # since some tests inspect the dimensions actually passed through.
    from backend.ticket_rubric import get_default_ticket_rubric

    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {
            "id": "rubric-id", "name": "Ticket QA", "version": 1,
            "dimensions": get_default_ticket_rubric(),
        },
    )


def test_score_400_when_ticket_ingestion_failed(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(status="failed"),
    )
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 400


def test_score_409_when_still_processing(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(status="processing"),
    )
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 409


def test_score_400_when_no_messages(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(messages=[]),
    )
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 400


def test_score_400_when_no_resolved_agents(auth_client, monkeypatch):
    """TA-25: scoring per-agent has nothing to run against when no turn
    on the ticket resolves to a real agent_user_id — a distinct 400 from
    the "no messages at all" case above."""
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(messages=[
            {"seq": 0, "speaker": "customer", "text": "hi",
             "agent_user_id": None, "sent_at": None, "has_image": False},
            {"seq": 1, "speaker": "agent", "text": "on it",
             "agent_user_id": None, "sent_at": None, "has_image": False},
        ]),
    )
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 400
    assert "agent identities" in r.json()["detail"]


def test_score_502_when_scoring_raises(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    _no_prior_audit(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("Claude call failed")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", _boom)
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 502


def test_score_success_shape(auth_client, monkeypatch):
    tid = str(uuid.uuid4())
    seen_org = {}

    def fake_get(ticket_id, org_id):
        seen_org["org_id"] = org_id
        assert ticket_id == tid
        return _fake_ticket()

    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", fake_get)
    _no_prior_audit(monkeypatch)

    def fake_score(turns, dimensions, *, only_agent_ids=None, **kwargs):
        assert dimensions[0]["id"] == "problem_diagnosis"
        assert turns[0]["seq"] == 0
        assert only_agent_ids == [AGENT_ID]
        return [{
            "agent_user_id": AGENT_ID,
            "score": 100.0,
            "spans": [],
            "findings": [
                {"id": "problem_diagnosis", "verdict": "pass",
                 "evidence_text": "Fixed the payment worker.", "evidence_seq": 1,
                 "evidence_verified": True, "attributed_to": AGENT_ID},
            ],
        }]

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", fake_score)
    r = auth_client.post(f"/api/tickets/{tid}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == tid
    assert body["cached"] is False
    assert len(body["agents"]) == 1
    agent_result = body["agents"][0]
    assert agent_result["agent_user_id"] == AGENT_ID
    assert agent_result["score"] == 100.0
    assert agent_result["findings"][0]["evidence_verified"] is True
    assert seen_org["org_id"] == DEFAULT_ORG_ID


def test_score_success_includes_audit_summary_and_top_strength_and_gap(auth_client, monkeypatch):
    """IN-12: computed from each agent's own scoring output, present in
    the response without a schema change elsewhere — _with_summary() adds
    them as plain top-level keys on that agent's entry."""
    tid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    _no_prior_audit(monkeypatch)

    def fake_score(turns, dimensions, *, only_agent_ids=None, **kwargs):
        return [{
            "agent_user_id": AGENT_ID,
            "score": 60.0,
            "spans": [],
            "findings": [
                {"id": "tone", "name": "Tone", "weight": 15, "verdict": "pass",
                 "reasoning": "Stayed professional.", "evidence_text": "Thanks!",
                 "evidence_seq": 1, "evidence_verified": True, "attributed_to": AGENT_ID},
                {"id": "investigation_rigor", "name": "Investigation Rigor", "weight": 20,
                 "verdict": "fail", "reasoning": "Guessed instead of checking logs.",
                 "evidence_text": "Try restarting.", "evidence_seq": 0,
                 "evidence_verified": True, "attributed_to": AGENT_ID},
            ],
        }]

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", fake_score)
    r = auth_client.post(f"/api/tickets/{tid}/score")
    assert r.status_code == 200
    agent_result = r.json()["agents"][0]
    assert agent_result["top_strength"]["id"] == "tone"
    assert agent_result["top_gap"]["id"] == "investigation_rigor"
    assert "Tone" in agent_result["audit_summary"]
    assert "Investigation Rigor" in agent_result["audit_summary"]


def test_score_route_filters_agents_for_a_viewer_not_on_this_ticket(auth_client, monkeypatch):
    """A non-manager who isn't one of the ticket's own resolved agents
    gets an empty agents list — never a teammate's individual scorecard,
    even on a ticket they can see (filter_audits_for_viewer, TA-29)."""
    tid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    monkeypatch.setattr("backend.ticket_score_api.auth.is_owner_or_manager", lambda request: False)
    _no_prior_audit(monkeypatch)

    def fake_score(turns, dimensions, *, only_agent_ids=None, **kwargs):
        return [{
            "agent_user_id": AGENT_ID, "score": 60.0, "spans": [],
            "findings": [{"id": "tone", "verdict": "pass", "attributed_to": AGENT_ID}],
        }]

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", fake_score)
    r = auth_client.post(f"/api/tickets/{tid}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "own"
    assert body["agents"] == []


def test_score_route_shows_own_scorecard_to_the_resolved_agent_themself(monkeypatch):
    """The flip side of the above: a non-manager who *is* the resolved
    agent on this ticket sees their own scorecard."""
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    tid = str(uuid.uuid4())
    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_ID)
    monkeypatch.setattr("backend.ticket_score_api.auth.is_owner_or_manager", lambda request: False)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    _no_prior_audit(monkeypatch)

    def fake_score(turns, dimensions, *, only_agent_ids=None, **kwargs):
        return [{
            "agent_user_id": AGENT_ID, "score": 60.0, "spans": [],
            "findings": [{"id": "tone", "verdict": "pass", "attributed_to": AGENT_ID}],
        }]

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket_per_agent", fake_score)
    r = client.post(f"/api/tickets/{tid}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["view_scope"] == "own"
    assert len(body["agents"]) == 1
    assert body["agents"][0]["agent_user_id"] == AGENT_ID


def test_score_route_is_registered_on_the_app():
    from backend.api import app

    paths = {r.path for r in app.routes}
    assert "/api/tickets/{ticket_id}/score" in paths


# ---------- live: real upload -> real score, through the actual HTTP routes ----------


def test_upload_then_score_live_end_to_end(monkeypatch):
    """The exact path TA-10's frontend will drive: POST a real PDF to
    /api/tickets/upload, map its agent name to a real org member (TA-15,
    required since TA-25's per-agent scoring 400s with nothing resolved),
    then POST /api/tickets/{id}/score — real Postgres, real Storage, real
    Claude vision + text scoring calls, no mocks on the ticket-engine
    side. Only auth is stubbed (conftest's own convention), scoped to a
    throwaway org, cleaned up after."""
    import psycopg
    from dotenv import dotenv_values
    from fastapi.testclient import TestClient
    from PIL import Image
    from psycopg.rows import dict_row

    from backend.api import app
    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE
    from tests.conftest import authorize
    from tests.test_ticket_ingest import _build_justcall_pdf_with_image

    raw_env = dotenv_values(ENV_FILE)
    real_supabase_url = raw_env.get("SUPABASE_URL")
    real_service_role_key = raw_env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not real_supabase_url or not real_service_role_key or "test.supabase.co" in real_supabase_url:
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    # Patch ticket_image_store's two lowest-level accessors directly rather
    # than the SUPABASE_URL env var: auth.py verifies this test's JWT
    # against conftest's fake TEST_SUPABASE_URL baked into its `iss` claim,
    # so overriding the env var globally would fail auth. Every other
    # ticket_image_store function (put_bytes, signed_url, ensure_bucket,
    # configured) calls through these two, so this alone redirects all of
    # it to the real project without touching auth at all.
    from backend import ticket_image_store
    monkeypatch.setattr(ticket_image_store, "_supabase_url", lambda: real_supabase_url.rstrip("/"))
    monkeypatch.setattr(ticket_image_store, "_service_role_key", lambda: real_service_role_key)

    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        """
        SELECT to_regclass('public.tickets') AS t,
               to_regclass('public.ticket_audits') AS a,
               to_regclass('public.ticket_agent_aliases') AS al
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["a"] or not exists["al"]:
        admin.close()
        pytest.skip("0039_ticket_audits_per_agent / 0025_ticket_agent_aliases not applied")

    org_id = str(uuid.uuid4())
    agent_user_id = str(uuid.uuid4())
    ticket_id = None
    asset_seq = None
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta10-route-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, %s)",
            (org_id, agent_user_id, "member"),
        )
        admin.commit()

        img = Image.new("RGB", (100, 50), (30, 30, 30))
        pdf_bytes = _build_justcall_pdf_with_image(
            "Conversation with JustCall\nTicket Details\n"
            "--- August 19, 2026 ---\n"
            "06:16 AM | Kevin Abraham: Checkout is returning error code 504\n"
            "06:20 AM | Tanu from JustCall: I can see the 504, deploying a fix now\n"
            "Exported from JustCall on September 5, 2026 at 03:46 AM",
            img,
        )

        client = TestClient(app)
        authorize(client, monkeypatch, org_id=org_id)

        upload_resp = client.post(
            "/api/tickets/upload",
            files={"file": ("ticket.pdf", pdf_bytes, "application/pdf")},
        )
        assert upload_resp.status_code == 200, upload_resp.text
        ticket_id = upload_resp.json()["ticket_id"]

        from backend import ticket_agent_aliases
        alias_result = ticket_agent_aliases.set_alias(org_id, "Tanu", agent_user_id)
        # 2, not 1: the image-derived turn (TA-5) is its own agent turn
        # under the same display name, alongside the original text reply.
        assert alias_result["backfilled_turns"] == 2

        score_resp = client.post(f"/api/tickets/{ticket_id}/score")
        assert score_resp.status_code == 200, score_resp.text
        body = score_resp.json()
        assert body["ticket_id"] == ticket_id
        assert body["cached"] is False
        assert len(body["agents"]) == 1
        agent_result = body["agents"][0]
        assert agent_result["agent_user_id"] == agent_user_id
        assert 0 <= agent_result["score"] <= 100
        # 5 Ticket QA dimensions (TA-24) + Response Timeliness (TA-13,
        # deterministic — appended fresh on every response, cached or not).
        assert len(agent_result["findings"]) == 6
        assert all("verdict" in f for f in agent_result["findings"])
        assert sum(1 for f in agent_result["findings"] if f.get("deterministic")) == 1

        again = client.post(f"/api/tickets/{ticket_id}/score")
        assert again.status_code == 200, again.text
        again_body = again.json()
        assert again_body["cached"] is True
        again_agent = again_body["agents"][0]
        assert again_agent["score"] == agent_result["score"]
        assert len(again_agent["findings"]) == 6

        blocked = client.post(f"/api/tickets/{ticket_id}/score", params={"refresh": "true"})
        assert blocked.status_code == 403

        get_resp = client.get(f"/api/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        messages = get_body["messages"]
        assert len(messages) == 3  # 2 text turns + 1 image-derived turn
        image_message = next(m for m in messages if m["has_image"])
        asset_seq = image_message["seq"]
        assert len(get_body["audits"]) == 1
        assert get_body["audits"][0]["agent_user_id"] == agent_user_id
        assert get_body["audits"][0]["score"] == body["agents"][0]["score"]

        asset_resp = client.get(f"/api/tickets/{ticket_id}/assets/{asset_seq}")
        assert asset_resp.status_code == 200
        assert asset_resp.json()["url"]
    finally:
        admin.execute("DELETE FROM ticket_audits WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_agent_aliases WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM api_usage WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
        if ticket_id and asset_seq is not None:
            try:
                import httpx
                key = ticket_image_store.object_key(org_id, ticket_id, asset_seq)
                httpx.request(
                    "DELETE",
                    f"{ticket_image_store._api_root()}/object/{ticket_image_store.bucket_name()}",
                    headers={**ticket_image_store._auth_headers(), "Content-Type": "application/json"},
                    json={"prefixes": [key]},
                    timeout=30.0,
                )
            except Exception:
                pass


# ---------- GET /api/tickets/rubric ----------


def test_ticket_rubric_route_requires_auth():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/tickets/rubric")
    assert r.status_code == 401


def test_ticket_rubric_route_returns_the_active_rubric(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    from tests.conftest import authorize

    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {
            "id": "r1", "name": "Ticket QA", "version": 1,
            "dimensions": [{"id": "tone", "name": "Tone", "weight": 15, "question": "Q?"}],
        },
    )
    r = client.get("/api/tickets/rubric")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Ticket QA"
    assert body["dimensions"][0]["id"] == "tone"


def test_ticket_rubric_route_is_not_swallowed_by_ticket_id_route(monkeypatch):
    """Regression guard: /api/tickets/rubric must be registered before
    /api/tickets/{ticket_id} — otherwise "rubric" gets treated as a
    ticket_id and this 400s instead of ever reaching this route."""
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    from tests.conftest import authorize

    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {"id": "r1", "name": "Ticket QA", "version": 1, "dimensions": []},
    )
    r = client.get("/api/tickets/rubric")
    assert r.status_code == 200
    assert r.json()["dimensions"] == []
