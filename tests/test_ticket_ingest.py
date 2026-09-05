"""TA-4 write path: parsed ticket turns -> tickets / ticket_messages (TA-3).

Fake-connection unit tests for the write logic itself, plus one live
Postgres test (skipped without DATABASE_URL, or if 0022_tickets isn't
applied) that proves the whole thing end-to-end against a real database —
using its own temporary org, deleted in a finally block, never touching
any pre-existing row."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

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
        ("t1", ORG_A, 0, None, "customer", "hi"),
        ("t1", ORG_A, 1, "u1", "agent", "hello"),
        ("t1", ORG_A, 2, None, "bot", "beep"),
    ]


def test_insert_ticket_messages_is_a_noop_for_no_turns(monkeypatch):
    from backend import ticket_ingest

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_ingest.insert_ticket_messages("t1", ORG_A, [])
    assert conn.messages == []


def test_ingest_ticket_pdf_moves_through_uploaded_processing_ready(monkeypatch):
    from backend import ticket_ingest

    monkeypatch.setattr(
        ticket_ingest.ticket_pdf_parser, "parse_ticket_pdf",
        lambda pdf_bytes: [
            {"seq": 0, "speaker": "customer", "speaker_name": "Kevin", "agent_user_id": None, "text": "hi"},
        ],
    )
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_id = ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert [s for _tid, s in conn.status_updates] == ["processing", "ready"]
    assert len(conn.messages) == 1
    assert conn.tickets[0]["id"] == ticket_id


def test_ingest_ticket_pdf_marks_failed_and_reraises_on_unknown_format(monkeypatch):
    from backend import ticket_ingest

    def _boom(pdf_bytes):
        raise ValueError("PDF does not match the known JustCall export template")

    monkeypatch.setattr(ticket_ingest.ticket_pdf_parser, "parse_ticket_pdf", _boom)
    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(ValueError):
            ticket_ingest.ingest_ticket_pdf(ORG_A, b"fake-pdf-bytes")
    assert [s for _tid, s in conn.status_updates] == ["processing", "failed"]
    assert conn.messages == []  # never wrote turns for a failed parse


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
