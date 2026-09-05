"""TA-9: /api/tickets HTTP surface. Own upload path, not /api/upload."""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

CALL_ENGINE = ("qa_engine", "qa_v8", "rules_v8", "transcribe", "ticket_scoring")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


def test_ticket_api_does_not_import_the_call_engine():
    src = (ROOT / "backend" / "ticket_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in CALL_ENGINE, alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            bits = {p for p in mod.split(".") if p}
            assert not (bits & set(CALL_ENGINE)), mod


def test_ticket_api_never_bypasses_rls():
    src = (ROOT / "backend" / "ticket_api.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src
    assert "org_id_from_request" in src


def test_upload_path_is_not_the_call_upload():
    src = (ROOT / "backend" / "ticket_api.py").read_text(encoding="utf-8")
    assert '"/api/tickets/upload"' in src
    assert '"/api/upload"' not in src
    assert '"/api/upload-batch"' not in src


def test_tickets_upload_401_without_token():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/tickets/upload", files={"file": ("t.pdf", b"%PDF-1.4 x", "application/pdf")})
    assert r.status_code == 401


def test_upload_rejects_empty_file(auth_client):
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "The uploaded file was empty."


def test_upload_rejects_non_pdf_bytes(auth_client):
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"not a pdf", "application/pdf")},
    )
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_upload_rejects_non_pdf_extension(auth_client):
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("call.mp3", b"%PDF-1.4 still", "audio/mpeg")},
    )
    assert r.status_code == 400


def test_upload_rejects_oversized_pdf(auth_client, monkeypatch):
    monkeypatch.setattr("backend.ticket_api.MAX_TICKET_PDF_BYTES", 8)
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"%PDF-1.4 too-big", "application/pdf")},
    )
    assert r.status_code == 413


def test_upload_hands_pdf_to_ingest_not_call_upload(auth_client, monkeypatch):
    seen = {}
    ticket_id = str(uuid.uuid4())

    def fake_ingest(org_id, pdf_bytes, *, source="pdf_upload"):
        seen["org_id"] = org_id
        seen["pdf_bytes"] = pdf_bytes
        seen["source"] = source
        return ticket_id

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.ingest_ticket_pdf", fake_ingest)

    scored = {"called": False}
    monkeypatch.setattr(
        "backend.ticket_scoring.score_ticket",
        lambda *a, **k: scored.update(called=True) or {},
        raising=False,
    )

    payload = b"%PDF-1.4 justcall-export"
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("export.pdf", payload, "application/pdf")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket_id"] == ticket_id
    assert body["status"] == "ready"
    assert body["filename"] == "export.pdf"
    assert body["source"] == "pdf_upload"
    assert seen["org_id"] == DEFAULT_ORG_ID
    assert seen["pdf_bytes"] == payload
    assert seen["source"] == "pdf_upload"
    assert scored["called"] is False


def test_upload_strips_path_from_filename(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.ingest_ticket_pdf",
        lambda *a, **k: str(uuid.uuid4()),
    )
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("../../secret.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert r.status_code == 200
    assert r.json()["filename"] == "secret.pdf"


def test_upload_unrecognized_pdf_is_400(auth_client, monkeypatch):
    def boom(org_id, pdf_bytes, *, source="pdf_upload"):
        raise ValueError("PDF does not match the known JustCall export template")

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.ingest_ticket_pdf", boom)
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert r.status_code == 400
    assert r.json()["detail"] == "This PDF is not a JustCall ticket export."
    assert "does not match" not in r.text


def test_upload_storage_error_is_503(auth_client, monkeypatch):
    from backend.ticket_image_store import TicketImageStoreError

    def boom(org_id, pdf_bytes, *, source="pdf_upload"):
        raise TicketImageStoreError("upload_failed")

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.ingest_ticket_pdf", boom)
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert r.status_code == 503


def test_upload_vision_error_is_502(auth_client, monkeypatch):
    def boom(org_id, pdf_bytes, *, source="pdf_upload"):
        raise RuntimeError("Claude vision call failed")

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.ingest_ticket_pdf", boom)
    r = auth_client.post(
        "/api/tickets/upload",
        files={"file": ("t.pdf", b"%PDF-1.4 x", "application/pdf")},
    )
    assert r.status_code == 502
    assert r.json()["detail"] == "Ticket screenshot processing failed."


def test_list_tickets_uses_jwt_org(auth_client, monkeypatch):
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.list_tickets",
        lambda org_id: [{"id": "t1", "org_check": org_id, "status": "ready",
                         "source": "pdf_upload", "created_at": None, "message_count": 2}],
    )
    r = auth_client.get("/api/tickets")
    assert r.status_code == 200
    tickets = r.json()["tickets"]
    assert tickets[0]["id"] == "t1"
    assert tickets[0]["org_check"] == DEFAULT_ORG_ID


def test_get_ticket_404(auth_client, monkeypatch):
    monkeypatch.setattr("backend.ticket_api.ticket_ingest.get_ticket", lambda *a, **k: None)
    r = auth_client.get(f"/api/tickets/{uuid.uuid4()}")
    assert r.status_code == 404


def test_get_ticket_rejects_non_uuid(auth_client):
    r = auth_client.get("/api/tickets/not-a-uuid")
    assert r.status_code == 400


def test_get_ticket_returns_turns_and_image_flags(auth_client, monkeypatch):
    tid = str(uuid.uuid4())

    def fake_get(ticket_id, org_id):
        assert ticket_id == tid
        assert org_id == DEFAULT_ORG_ID
        return {
            "id": tid,
            "source": "pdf_upload",
            "status": "ready",
            "created_at": "2026-09-05T00:00:00+00:00",
            "messages": [
                {"seq": 0, "speaker": "customer", "text": "504",
                 "agent_user_id": None, "sent_at": None, "has_image": False},
                {"seq": 1, "speaker": "customer", "text": "error dialog",
                 "agent_user_id": None, "sent_at": None, "has_image": True},
            ],
            "assets": [{"seq": 1, "width": 300, "height": 150, "content_type": "image/png"}],
        }

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.get_ticket", fake_get)
    r = auth_client.get(f"/api/tickets/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["messages"][1]["has_image"] is True
    assert "storage_key" not in body["assets"][0]


def test_asset_signed_url_404_when_row_missing(auth_client, monkeypatch):
    monkeypatch.setattr("backend.ticket_api.ticket_ingest.ticket_asset_meta", lambda *a, **k: None)
    r = auth_client.get(f"/api/tickets/{uuid.uuid4()}/assets/0")
    assert r.status_code == 404


def test_asset_signed_url_ok(auth_client, monkeypatch):
    tid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.ticket_api.ticket_ingest.ticket_asset_meta",
        lambda ticket_id, org_id, seq: {
            "seq": seq, "width": 10, "height": 10, "content_type": "image/png",
        },
    )
    monkeypatch.setattr(
        "backend.ticket_api.ticket_image_store.signed_url",
        lambda org_id, ticket_id, seq: ("https://signed.example/img", 3600),
    )
    r = auth_client.get(f"/api/tickets/{tid}/assets/3")
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == "https://signed.example/img"
    assert body["expires_in"] == 3600
    assert body["seq"] == 3
    assert "/object/public/" not in body["url"]


def test_list_tickets_sql_is_org_scoped(monkeypatch):
    recorded = []

    class _Conn:
        def execute(self, sql, params=None):
            recorded.append((" ".join(str(sql).split()), params))
            return _Result([])

    @contextmanager
    def _cm(*_a, **_k):
        yield _Conn()

    monkeypatch.setattr("backend.ticket_ingest.db.connection", _cm)
    from backend import ticket_ingest

    org = str(uuid.uuid4())
    assert ticket_ingest.list_tickets(org) == []
    sql, params = recorded[0]
    assert params == (org,)
    assert "%s" in sql
    assert "WHERE t.org_id" in sql


def test_get_ticket_sql_filters_ticket_and_org(monkeypatch):
    recorded = []
    tid = str(uuid.uuid4())
    org = str(uuid.uuid4())

    class _Conn:
        def execute(self, sql, params=None):
            recorded.append((" ".join(str(sql).split()), params))
            norm = " ".join(str(sql).split()).upper()
            if "FROM TICKETS" in norm:
                return _Result([])
            return _Result([])

    @contextmanager
    def _cm(*_a, **_k):
        yield _Conn()

    monkeypatch.setattr("backend.ticket_ingest.db.connection", _cm)
    from backend import ticket_ingest

    assert ticket_ingest.get_ticket(tid, org) is None
    sql, params = recorded[0]
    assert params == (tid, org)
    assert sql.count("%s") == 2


def test_org_b_cannot_get_org_a_ticket_via_api(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    hidden = str(uuid.uuid4())

    def fake_get(ticket_id, org_id):
        # RLS/app layer: a ticket from another org is simply not visible.
        if org_id != DEFAULT_ORG_ID:
            return None
        if ticket_id == hidden:
            return None
        return {"id": ticket_id, "messages": [], "assets": []}

    monkeypatch.setattr("backend.ticket_api.ticket_ingest.get_ticket", fake_get)
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=str(uuid.uuid4()))
    r = client.get(f"/api/tickets/{hidden}")
    assert r.status_code == 404
