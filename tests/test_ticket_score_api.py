"""POST /api/tickets/{ticket_id}/score — wires TA-6 (ticket_scoring.py) +
TA-7 (ticket_rubric.py, scaffold) to an HTTP route. Neither TA-9's upload
route nor any other route did this; ticket_score_api.py fills that gap
so TA-10 has a real endpoint to render."""

from __future__ import annotations

import ast
import uuid

import pytest

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT

CALL_ENGINE = ("qa_engine", "qa_v8", "rules_v8", "transcribe")


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


def test_uses_the_scaffold_rubric_not_a_hardcoded_one():
    src = (ROOT / "backend" / "ticket_score_api.py").read_text(encoding="utf-8")
    assert "ticket_rubric" in src
    assert "get_scaffold_rubric" in src


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
             "agent_user_id": None, "sent_at": None, "has_image": False},
        ],
        "assets": [],
        "audit": None,
    }


def _no_prior_audit(monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.fetch_latest",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_audit_store.upsert",
        lambda *a, **k: "audit-id",
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


def test_score_502_when_scoring_raises(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_score_api.ticket_ingest.get_ticket",
        lambda *a, **k: _fake_ticket(),
    )
    _no_prior_audit(monkeypatch)

    def _boom(*_a, **_k):
        raise RuntimeError("Claude call failed")

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", _boom)
    r = auth_client.post(f"/api/tickets/{uuid.uuid4()}/score")
    assert r.status_code == 502


def test_score_success_shape_and_scaffold_flag(auth_client, monkeypatch):
    tid = str(uuid.uuid4())
    seen_org = {}

    def fake_get(ticket_id, org_id):
        seen_org["org_id"] = org_id
        assert ticket_id == tid
        return _fake_ticket()

    monkeypatch.setattr("backend.ticket_score_api.ticket_ingest.get_ticket", fake_get)
    _no_prior_audit(monkeypatch)

    def fake_score(turns, dimensions, **kwargs):
        assert dimensions[0]["scaffold"] is True
        assert turns[0]["seq"] == 0
        return {
            "score": 100.0,
            "primary_owner": None,
            "spans": [],
            "findings": [
                {"id": "diagnostic_reasoning", "verdict": "pass",
                 "evidence_text": "Fixed the payment worker.", "evidence_seq": 1,
                 "evidence_verified": True, "attributed_to": None},
            ],
        }

    monkeypatch.setattr("backend.ticket_score_api.ticket_scoring.score_ticket", fake_score)
    r = auth_client.post(f"/api/tickets/{tid}/score")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket_id"] == tid
    assert body["rubric_scaffold"] is True
    assert body["cached"] is False
    assert body["score"] == 100.0
    assert body["findings"][0]["evidence_verified"] is True
    assert seen_org["org_id"] == DEFAULT_ORG_ID


def test_score_route_is_registered_on_the_app():
    from backend.api import app

    paths = {r.path for r in app.routes}
    assert "/api/tickets/{ticket_id}/score" in paths


# ---------- live: real upload -> real score, through the actual HTTP routes ----------


def test_upload_then_score_live_end_to_end(monkeypatch):
    """The exact path TA-10's frontend will drive: POST a real PDF to
    /api/tickets/upload, then POST /api/tickets/{id}/score — real
    Postgres, real Storage, real Claude vision + text scoring calls, no
    mocks on the ticket-engine side. Only auth is stubbed (conftest's own
    convention), scoped to a throwaway org, cleaned up after."""
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
               to_regclass('public.ticket_audits') AS a
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["a"]:
        admin.close()
        pytest.skip("0024_ticket_audits not applied")

    org_id = str(uuid.uuid4())
    ticket_id = None
    asset_seq = None
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta10-route-live-test"))
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

        score_resp = client.post(f"/api/tickets/{ticket_id}/score")
        assert score_resp.status_code == 200, score_resp.text
        body = score_resp.json()
        assert body["ticket_id"] == ticket_id
        assert body["rubric_scaffold"] is True
        assert body["cached"] is False
        assert 0 <= body["score"] <= 100
        assert len(body["findings"]) == 6  # the six scaffold dimensions
        assert all("verdict" in f for f in body["findings"])

        again = client.post(f"/api/tickets/{ticket_id}/score")
        assert again.status_code == 200, again.text
        assert again.json()["cached"] is True
        assert again.json()["score"] == body["score"]

        blocked = client.post(f"/api/tickets/{ticket_id}/score", params={"refresh": "true"})
        assert blocked.status_code == 403

        get_resp = client.get(f"/api/tickets/{ticket_id}")
        assert get_resp.status_code == 200
        messages = get_resp.json()["messages"]
        assert len(messages) == 3  # 2 text turns + 1 image-derived turn
        image_message = next(m for m in messages if m["has_image"])
        asset_seq = image_message["seq"]
        assert get_resp.json()["audit"] is not None
        assert get_resp.json()["audit"]["score"] == body["score"]

        asset_resp = client.get(f"/api/tickets/{ticket_id}/assets/{asset_seq}")
        assert asset_resp.status_code == 200
        assert asset_resp.json()["url"]
    finally:
        admin.execute("DELETE FROM ticket_audits WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM api_usage WHERE org_id = %s", (org_id,))
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
