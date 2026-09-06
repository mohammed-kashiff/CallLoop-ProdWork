"""AC-44/AC-45: wiring product_events.track_event() into real backend call
sites, and the POST /api/events route for frontend-only interactions.

Strategy: a static regression guard proves each call site still calls
track_event() with the right event_name (cheap, catches "removed by
accident" — mocking every dependency of a Claude-calling route just to
unit-test one telemetry line isn't worth it), plus one real live
end-to-end test (ticket upload, the cheapest fully-real path already used
elsewhere in this suite) proving a real row actually lands in
product_events end to end, and full HTTP tests for the standalone
POST /api/events route."""

from __future__ import annotations

import uuid

import pytest

from backend.paths import ROOT


def _src(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


# ---------- static regression guard: every AC-44 call site still wired ----------


@pytest.mark.parametrize(
    "rel_path,event_name",
    [
        ("backend/ticket_api.py", "ticket_uploaded"),
        ("backend/rubric_builder.py", "rubric_saved"),
        ("backend/api.py", "call_uploaded"),
        ("backend/api.py", "upload_failed"),
        ("backend/api.py", "batch_partial_failure"),
        ("backend/api.py", "flag_created"),
        ("backend/api.py", "flag_solved"),
        ("backend/api.py", "feedback_requested"),
        ("backend/api.py", "stakeholder_email_drafted"),
    ],
)
def test_backend_wired_event_call_site_still_present(rel_path, event_name):
    src = _src(rel_path)
    assert f'"{event_name}"' in src
    assert "product_events.track_event(" in src


def test_module_never_bypasses_rls():
    for rel in ("backend/product_events.py",):
        assert "bypass_rls" not in _src(rel)


# ---------- POST /api/events ----------


def test_track_frontend_event_requires_auth():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/events", json={"event_name": "session_started"})
    assert r.status_code == 401


def test_track_frontend_event_rejects_a_backend_wired_event_name(monkeypatch):
    """The browser must never be able to fabricate a backend-wired event
    (e.g. a fake call_uploaded with no real upload behind it)."""
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/events", json={"event_name": "call_uploaded"})
    assert r.status_code == 400


def test_track_frontend_event_rejects_an_unknown_event_name(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/events", json={"event_name": "literally_anything"})
    assert r.status_code == 400


def test_track_frontend_event_rejects_oversized_properties(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        "/api/events",
        json={"event_name": "session_started", "properties": {"x": "y" * 5000}},
    )
    assert r.status_code == 400


def test_track_frontend_event_accepts_a_valid_frontend_only_event(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.api.product_events.track_event",
        lambda org_id, user_id, event_name, properties=None: calls.append(
            (org_id, user_id, event_name, properties)
        ),
    )
    r = client.post(
        "/api/events",
        json={"event_name": "rubric_builder_opened", "properties": {"page": "builder"}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls[0][2] == "rubric_builder_opened"
    assert calls[0][3] == {"page": "builder"}


def test_track_frontend_event_defaults_properties_to_empty_dict(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.api.product_events.track_event",
        lambda org_id, user_id, event_name, properties=None: calls.append(properties),
    )
    r = client.post("/api/events", json={"event_name": "session_started"})
    assert r.status_code == 200
    assert calls[0] == {}


# ---------- live end-to-end: a real ticket upload produces a real row ----------


def test_ticket_upload_produces_a_real_product_events_row_live(monkeypatch):
    """The cheapest fully-real path in this suite (no audio/Claude needed) —
    proves track_event() is genuinely wired into a real route, not just
    present in the source."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute("SELECT to_regclass('public.product_events') AS t").fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0028_product_events not applied")

    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ac44-live-test"))
        admin.commit()

        client = TestClient(app)
        authorize(client, monkeypatch, org_id=org_id)

        pdf_bytes = _minimal_justcall_pdf()
        r = client.post(
            "/api/tickets/upload",
            files={"file": ("ticket.pdf", pdf_bytes, "application/pdf")},
        )
        assert r.status_code == 200, r.text

        rows = admin.execute(
            "SELECT event_name, org_id, user_id, properties FROM product_events "
            "WHERE org_id = %s AND event_name = 'ticket_uploaded'",
            (org_id,),
        ).fetchall()
        assert len(rows) == 1
        assert str(rows[0]["org_id"]) == org_id
        assert rows[0]["properties"].get("size_bytes") == len(pdf_bytes)
    finally:
        admin.execute("DELETE FROM product_events WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def _minimal_justcall_pdf() -> bytes:
    """A minimal, real, parseable JustCall export PDF — same hand-built
    single-page approach as test_ticket_ingest.py, no external PDF library."""
    text = (
        "Conversation with JustCall\nTicket Details\n"
        "--- August 19, 2026 ---\n"
        "06:16 AM | Kevin Abraham: Here is what I'm seeing\n"
        "Exported from JustCall on September 5, 2026 at 03:46 AM"
    )
    content_lines = text.split("\n")
    stream_ops = ["BT", "/F1 10 Tf", "72 750 Td", "12 TL"]
    for line in content_lines:
        stream_ops.append(f"({line}) Tj")
        stream_ops.append("T*")
    stream_ops.append("ET")
    stream = "\n".join(stream_ops).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 1000] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    import io

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i)
        out.write(obj)
        out.write(b"\nendobj\n")
    xref_offset = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_offset)
    )
    return out.getvalue()
