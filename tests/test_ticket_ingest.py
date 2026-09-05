"""TA-4/TA-5 write path: parsed ticket turns + embedded images ->
tickets / ticket_messages (TA-3) / ticket_message_assets (TA-5).

Fake-connection unit tests for the write logic itself, plus live Postgres
+ Storage tests (skipped without DATABASE_URL / SUPABASE creds, or if the
relevant migration isn't applied) that prove the whole thing end-to-end —
each using its own temporary org, cleaned up in a finally block, never
touching any pre-existing row or object."""

from __future__ import annotations

import io
import uuid
import zlib
from contextlib import contextmanager

import pytest
from PIL import Image

from backend.paths import ROOT


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self):
        self.tickets: list[dict] = []
        self.messages: list[tuple] = []
        self.assets: list[tuple] = []
        self.status_updates: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("INSERT INTO TICKETS"):
            org_id, source = params
            new_id = str(uuid.uuid4())
            self.tickets.append({"id": new_id, "org_id": org_id, "source": source})
            return _Result([{"id": new_id}])
        if norm.startswith("UPDATE TICKETS SET STATUS"):
            status, ticket_id, org_id = params
            self.status_updates.append((ticket_id, status))
            return _Result([])
        if norm.startswith("INSERT INTO TICKET_MESSAGE_ASSETS"):
            self.assets.append(params)
            return _Result([])
        if norm.startswith("INSERT INTO TICKET_MESSAGES"):
            self.messages.append(params)
            return _Result([])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.ticket_ingest.db.connection", _cm)
    monkeypatch.setattr(
        "backend.ticket_ingest.org_scope",
        lambda oid: _cm(),
    )
    yield conn


ORG_A = str(uuid.uuid4())


def test_create_ticket_inserts_with_default_source_and_returns_id(monkeypatch):
    from backend import ticket_ingest

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_id = ticket_ingest.create_ticket(ORG_A, source="pdf_upload")
    assert conn.tickets == [{"id": ticket_id, "org_id": ORG_A, "source": "pdf_upload"}]


def test_set_ticket_status_updates_the_right_row(monkeypatch):
    from backend import ticket_ingest

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.set_ticket_status("t1", ORG_A, "processing")
    assert conn.status_updates == [("t1", "processing")]


def test_insert_ticket_messages_writes_one_row_per_turn_in_order(monkeypatch):
    from backend import ticket_ingest

    turns = [
        {"seq": 0, "speaker": "customer", "speaker_name": "Kevin", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "speaker_name": "Kashif", "agent_user_id": "u1", "text": "hello"},
        {"seq": 2, "speaker": "bot", "speaker_name": "Welma Bot", "agent_user_id": None, "text": "beep"},
    ]
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.insert_ticket_messages("t1", ORG_A, turns)
    assert conn.messages == [
        ("t1", ORG_A, 0, None, "customer", "hi", None),
        ("t1", ORG_A, 1, "u1", "agent", "hello", None),
        ("t1", ORG_A, 2, None, "bot", "beep", None),
    ]


def test_insert_ticket_messages_is_a_noop_for_no_turns(monkeypatch):
    from backend import ticket_ingest

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.insert_ticket_messages("t1", ORG_A, [])
    assert conn.messages == []


def test_insert_ticket_message_assets_writes_one_row_per_asset(monkeypatch):
    from backend import ticket_ingest

    assets = [
        {"seq": 1, "width": 300, "height": 150, "storage_key": f"{ORG_A}/t1/1.png"},
    ]
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.insert_ticket_message_assets("t1", ORG_A, assets)
    assert conn.assets == [("t1", ORG_A, 1, 300, 150, f"{ORG_A}/t1/1.png")]


def test_insert_ticket_message_assets_is_a_noop_for_no_assets(monkeypatch):
    from backend import ticket_ingest

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.insert_ticket_message_assets("t1", ORG_A, [])
    assert conn.assets == []


# ---------- interleave_images: pure function, no DB/network ----------


def _text_turn(seq, speaker, page_index, text="hi", speaker_name=None, agent_user_id=None):
    return {
        "seq": seq, "speaker": speaker, "speaker_name": speaker_name or speaker,
        "agent_user_id": agent_user_id, "text": text, "page_index": page_index,
    }


def _image(page_index, image_index=0, width=100, height=50):
    return {
        "page_index": page_index, "image_index": image_index,
        "width": width, "height": height, "png_bytes": b"fake-png",
    }


def test_interleave_images_inserts_image_after_the_turn_on_the_same_page():
    from backend import ticket_ingest

    turns = [
        _text_turn(0, "customer", page_index=0),
        _text_turn(1, "agent", page_index=0),
        _text_turn(2, "agent", page_index=1),
    ]
    images = [_image(page_index=0)]
    merged = ticket_ingest.interleave_images(turns, images, ["a screenshot"])

    assert [t["is_image"] for t in merged] == [False, False, True, False]
    assert merged[2]["text"] == "a screenshot"
    assert merged[2]["speaker"] == "agent"  # inherits the preceding turn's speaker
    assert [t["seq"] for t in merged] == [0, 1, 2, 3]


def test_interleave_images_appends_trailing_images_after_the_last_turn():
    from backend import ticket_ingest

    turns = [_text_turn(0, "customer", page_index=0)]
    images = [_image(page_index=5)]  # a page far beyond any turn
    merged = ticket_ingest.interleave_images(turns, images, ["desc"])
    assert merged[-1]["is_image"] is True
    assert merged[-1]["speaker"] == "customer"


def test_interleave_images_handles_multiple_images_on_the_same_page_in_order():
    from backend import ticket_ingest

    turns = [_text_turn(0, "agent", page_index=0)]
    images = [_image(page_index=0, image_index=0), _image(page_index=0, image_index=1)]
    merged = ticket_ingest.interleave_images(turns, images, ["first", "second"])
    image_turns = [t for t in merged if t["is_image"]]
    assert [t["text"] for t in image_turns] == ["first", "second"]


def test_interleave_images_with_no_images_returns_turns_unchanged_but_tagged():
    from backend import ticket_ingest

    turns = [_text_turn(0, "customer", page_index=0), _text_turn(1, "agent", page_index=0)]
    merged = ticket_ingest.interleave_images(turns, [], [])
    assert len(merged) == 2
    assert all(t["is_image"] is False for t in merged)
    assert [t["seq"] for t in merged] == [0, 1]


def test_interleave_images_with_no_turns_falls_back_to_customer_speaker():
    from backend import ticket_ingest

    merged = ticket_ingest.interleave_images([], [_image(page_index=0)], ["desc"])
    assert len(merged) == 1
    assert merged[0]["speaker"] == "customer"
    assert merged[0]["is_image"] is True


def test_interleave_images_carries_width_height_png_bytes_for_storage():
    from backend import ticket_ingest

    turns = [_text_turn(0, "agent", page_index=0)]
    images = [_image(page_index=0, width=640, height=480)]
    merged = ticket_ingest.interleave_images(turns, images, ["desc"])
    image_turn = next(t for t in merged if t["is_image"])
    assert image_turn["width"] == 640
    assert image_turn["height"] == 480
    assert image_turn["png_bytes"] == b"fake-png"


# ---------- ingest_ticket_pdf: full pipeline, mocked parse/vision/storage ----------


def _patch_pipeline(monkeypatch, ticket_ingest, *, turns, images=None, descriptions=None,
                     justcall=True):
    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "extract_text", lambda pdf_bytes: "text")
    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "looks_like_justcall_export", lambda text: justcall)
    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "parse_turns_with_pages", lambda pdf_bytes: turns)
    monkeypatch.setattr(ticket_ingest.ticket_image_extraction, "extract_images", lambda pdf_bytes: images or [])
    descs = iter(descriptions or [])
    monkeypatch.setattr(ticket_ingest.ticket_image_extraction, "describe_image", lambda png_bytes: next(descs))
    monkeypatch.setattr(
        ticket_ingest.ticket_image_store, "put_bytes",
        lambda org_id, ticket_id, seq, png_bytes: f"{org_id}/{ticket_id}/{seq}.png",
    )


def test_ingest_ticket_pdf_moves_through_uploaded_processing_ready(monkeypatch):
    from backend import ticket_ingest

    _patch_pipeline(
        monkeypatch, ticket_ingest,
        turns=[_text_turn(0, "customer", page_index=0, text="hi")],
    )
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_id = ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert [s for _tid, s in conn.status_updates] == ["processing", "ready"]
    assert len(conn.messages) == 1
    assert conn.assets == []
    assert conn.tickets[0]["id"] == ticket_id


def test_ingest_ticket_pdf_marks_failed_and_reraises_on_unknown_format(monkeypatch):
    from backend import ticket_ingest

    _patch_pipeline(monkeypatch, ticket_ingest, turns=[], justcall=False)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(ValueError, match="JustCall export template"):
            ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert [s for _tid, s in conn.status_updates] == ["processing", "failed"]
    assert conn.messages == []
    assert conn.assets == []


def test_ingest_ticket_pdf_with_an_image_writes_message_and_asset_rows(monkeypatch):
    from backend import ticket_ingest

    _patch_pipeline(
        monkeypatch, ticket_ingest,
        turns=[_text_turn(0, "agent", page_index=0, text="here you go")],
        images=[_image(page_index=0, width=200, height=100)],
        descriptions=["a screenshot of an error"],
    )
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_id = ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert len(conn.messages) == 2  # the text turn + the injected image turn
    assert conn.messages[1][4] == "agent"  # image turn inherits speaker
    assert conn.messages[1][5] == "a screenshot of an error"
    assert len(conn.assets) == 1
    assert conn.assets[0] == (ticket_id, ORG_A, 1, 200, 100, f"{ORG_A}/{ticket_id}/1.png")
    assert [s for _tid, s in conn.status_updates] == ["processing", "ready"]


def test_ingest_ticket_pdf_marks_failed_on_a_vision_call_error(monkeypatch):
    from backend import ticket_ingest

    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "extract_text", lambda pdf_bytes: "text")
    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "looks_like_justcall_export", lambda text: True)
    monkeypatch.setattr(
        ticket_ingest.ticket_pdf_parser, "parse_turns_with_pages",
        lambda pdf_bytes: [_text_turn(0, "agent", page_index=0)],
    )
    monkeypatch.setattr(
        ticket_ingest.ticket_image_extraction, "extract_images",
        lambda pdf_bytes: [_image(page_index=0)],
    )

    def _boom(png_bytes):
        raise RuntimeError("Claude vision call failed after 3 attempts")

    monkeypatch.setattr(ticket_ingest.ticket_image_extraction, "describe_image", _boom)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(RuntimeError, match="Claude vision call failed"):
            ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert [s for _tid, s in conn.status_updates] == ["processing", "failed"]
    assert conn.messages == []  # nothing partial written
    assert conn.assets == []


def test_module_never_bypasses_rls():
    src = (ROOT / "backend" / "ticket_ingest.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src


# ---------- live Postgres: real end-to-end, own temp org, self-cleaning ----------


def test_ingest_ticket_pdf_live_end_to_end():
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import ticket_ingest
    from backend.org_ids import org_scope
    from backend.db import connection

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        """
        SELECT to_regclass('public.tickets') AS t,
               to_regclass('public.ticket_messages') AS m
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["m"]:
        admin.close()
        pytest.skip("0022_tickets not applied")

    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta4-live-test"))
        admin.commit()

        turns = [
            {"seq": 0, "speaker": "customer", "speaker_name": "Kevin Abraham",
             "agent_user_id": None, "text": "But why did it fail this time"},
            {"seq": 1, "speaker": "bot", "speaker_name": "Welma Bot",
             "agent_user_id": None, "text": "Hi Mike,\nYour campaign was rejected."},
            {"seq": 2, "speaker": "agent", "speaker_name": "Tanu",
             "agent_user_id": None, "text": "Hello Kevin, I understand your concern."},
        ]

        ticket_id = ticket_ingest.create_ticket(org_id, source="pdf_upload")
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
        ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")

        with org_scope(org_id):
            with connection() as conn:
                ticket_row = conn.execute(
                    "SELECT status, source FROM tickets WHERE id = %s", (ticket_id,),
                ).fetchone()
                assert ticket_row["status"] == "ready"
                assert ticket_row["source"] == "pdf_upload"

                msg_rows = conn.execute(
                    """
                    SELECT seq, speaker, text, agent_user_id FROM ticket_messages
                    WHERE ticket_id = %s ORDER BY seq
                    """,
                    (ticket_id,),
                ).fetchall()
                assert len(msg_rows) == 3
                assert [r["speaker"] for r in msg_rows] == ["customer", "bot", "agent"]
                assert msg_rows[1]["text"] == "Hi Mike,\nYour campaign was rejected."
                assert all(r["agent_user_id"] is None for r in msg_rows)

        other_org = str(uuid.uuid4())
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (other_org, "ta4-live-test-b"))
        admin.commit()
        with org_scope(other_org):
            with connection() as conn:
                seen = conn.execute(
                    "SELECT id FROM tickets WHERE org_id = %s", (org_id,),
                ).fetchall()
                assert seen == []
        admin.execute("DELETE FROM orgs WHERE id = %s", (other_org,))
        admin.commit()
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def _build_justcall_pdf_with_image(text: str, img: Image.Image) -> bytes:
    """Real one-page PDF with both a text content stream (the confirmed
    JustCall template) and one embedded raw RGB image XObject — hand-built
    (no external PDF-writing library), so ingest_ticket_pdf() can be
    exercised end to end exactly as it runs in production, rather than
    replicating its internals in the test."""
    w, h = img.size
    compressed = zlib.compress(img.convert("RGB").tobytes())
    content_lines = text.replace("(", r"\(").replace(")", r"\)").split("\n")
    stream_ops = ["BT", "/F1 10 Tf", "72 750 Td", "12 TL"]
    for line in content_lines:
        stream_ops.append(f"({line}) Tj")
        stream_ops.append("T*")
    stream_ops.append("ET")
    stream_ops.append(f"q {w} 0 0 {h} 72 100 cm /Im0 Do Q")
    stream = "\n".join(stream_ops).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R "
        b"/Resources << /Font << /F1 4 0 R >> /XObject << /Im0 6 0 R >> >> "
        b"/MediaBox [0 0 612 1600] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
         b"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
         b"/Length %d >>\nstream\n" % (w, h, len(compressed))) + compressed + b"\nendstream",
    ]
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


def test_ingest_ticket_pdf_full_live_end_to_end_with_a_real_image(monkeypatch):
    """Real Postgres + real Supabase Storage + real Claude vision call —
    proves TA-5's whole ingestion-time pipeline through the actual public
    entry point, ingest_ticket_pdf(), not a hand-assembled replica of it.
    The PDF is synthetic (built in-memory, never a committed fixture)
    since the real JustCall sample has no embedded images. Cleans up its
    temp org, ticket rows, and the uploaded Storage object."""
    import os

    from dotenv import dotenv_values

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    raw_env = dotenv_values(ENV_FILE)
    real_supabase_url = raw_env.get("SUPABASE_URL")
    real_service_role_key = raw_env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not real_supabase_url or not real_service_role_key or "test.supabase.co" in real_supabase_url:
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    # conftest.py stubs SUPABASE_URL to a fake host for the rest of the
    # suite (everything else there mocks httpx); scope the real value to
    # just this test via monkeypatch so it's reverted afterwards, rather
    # than load_dotenv(override=True) leaking a real credential into the
    # rest of the session's os.environ.
    monkeypatch.setenv("SUPABASE_URL", real_supabase_url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", real_service_role_key)
    for key, value in raw_env.items():
        if key not in os.environ and value is not None:
            monkeypatch.setenv(key, value)

    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    from backend import ticket_image_store
    if not ticket_image_store.configured():
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    import httpx
    import psycopg
    from psycopg.rows import dict_row

    from backend import ticket_ingest
    from backend.org_ids import org_scope
    from backend.db import connection

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        """
        SELECT to_regclass('public.tickets') AS t,
               to_regclass('public.ticket_messages') AS m,
               to_regclass('public.ticket_message_assets') AS a
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["m"] or not exists["a"]:
        admin.close()
        pytest.skip("0023_ticket_image_assets not applied")

    img = Image.new("RGB", (100, 50), (200, 0, 0))
    pdf_bytes = _build_justcall_pdf_with_image(
        "Conversation with JustCall\nTicket Details\n"
        "--- August 19, 2026 ---\n"
        "06:16 AM | Kevin Abraham: Here is what I'm seeing\n"
        "Exported from JustCall on September 5, 2026 at 03:46 AM",
        img,
    )

    org_id = str(uuid.uuid4())
    ticket_id = None
    asset = None
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta5-live-test"))
        admin.commit()

        ticket_id = ticket_ingest.ingest_ticket_pdf(org_id, pdf_bytes)

        with org_scope(org_id):
            with connection() as conn:
                ticket_row = conn.execute(
                    "SELECT status FROM tickets WHERE id = %s", (ticket_id,),
                ).fetchone()
                assert ticket_row["status"] == "ready"

                msg_rows = conn.execute(
                    "SELECT seq, speaker, text FROM ticket_messages "
                    "WHERE ticket_id = %s ORDER BY seq",
                    (ticket_id,),
                ).fetchall()
                assert len(msg_rows) == 2  # the text turn + the injected image turn
                assert msg_rows[0]["speaker"] == "customer"
                assert msg_rows[1]["speaker"] == "customer"  # image inherits the turn's speaker
                assert len(msg_rows[1]["text"]) > 0

                asset_rows = conn.execute(
                    "SELECT seq, width, height, storage_key FROM ticket_message_assets "
                    "WHERE ticket_id = %s",
                    (ticket_id,),
                ).fetchall()
                assert len(asset_rows) == 1
                asset = asset_rows[0]
                assert asset["seq"] == msg_rows[1]["seq"]
                assert asset["width"] == 100
                assert asset["height"] == 50

        # Prove the asset is genuinely viewable: mint a signed URL and
        # fetch real image bytes back from Storage.
        url, ttl = ticket_image_store.signed_url(org_id, ticket_id, asset["seq"])
        assert ttl >= 60
        r = httpx.get(url, timeout=30.0)
        assert r.status_code == 200
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
        if ticket_id and asset is not None:
            try:
                key = ticket_image_store.object_key(org_id, ticket_id, asset["seq"])
                httpx.request(
                    "DELETE",
                    f"{ticket_image_store._api_root()}/object/{ticket_image_store.bucket_name()}",
                    headers={**ticket_image_store._auth_headers(), "Content-Type": "application/json"},
                    json={"prefixes": [key]},
                    timeout=30.0,
                )
            except Exception:
                pass
