"""TA-8 (PRD §6): multi-agent attribution, v1 scope.

The mechanism (agent_spans / primary_owner / attributed_agent) already
lives in ticket_scoring.py — Cursor built it as part of TA-6, since
attribution falls out of the evidence-verification system that already
exists rather than needing a bespoke mechanism (that's the whole PRD
point). This file is TA-8's own verification, not a reimplementation:

1. Proves the PRD's exact named scenario (Agent 1 responds and goes off
   shift, the customer replies, Agent 2 picks up and resolves) end to
   end with real Postgres UUIDs — Cursor's own ticket_scoring tests only
   ever used string placeholders ("agent-a"/"agent-b"), never a genuine
   uuid.UUID as psycopg actually returns from an agent_user_id column,
   so equality/hashing across real UUID objects was never actually
   exercised until this test.
2. Documents, with a real ingested PDF, today's honest limitation: the
   PDF-upload MVP cannot resolve a display name to an org_members user
   id (TA-2's finding, called out in ticket_ingest.py's docstring), so a
   real two-agent bounce collapses into one undifferentiated span right
   now. That's expected behavior, not a bug — TA-3's schema is what
   makes the full mechanism ready for whenever identity resolution
   exists, without a rebuild.
"""

from __future__ import annotations

import uuid

import pytest


# ---------- 1. the exact PRD scenario, with real Postgres UUIDs ----------


def test_agent_bounce_scenario_attributes_correctly_with_real_postgres_uuids():
    """Agent 1 answers, goes off shift; customer replies; Agent 2 picks up
    and resolves. primary_owner must be Agent 2 (the resolving agent),
    but a finding whose evidence falls in Agent 1's span must still
    attribute to Agent 1 — attribution is per-finding, not "whoever
    closed the ticket gets credit for everything"."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import json

    import psycopg
    from psycopg.rows import dict_row

    from backend import ticket_ingest, ticket_scoring
    from backend.db import connection
    from backend.org_ids import org_scope

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        "SELECT to_regclass('public.ticket_messages') AS m"
    ).fetchone()
    if not exists or not exists["m"]:
        admin.close()
        pytest.skip("0022_tickets not applied")

    org_id = str(uuid.uuid4())
    agent_1 = str(uuid.uuid4())
    agent_2 = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta8-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'member'), (%s, %s, 'member')",
            (org_id, agent_1, org_id, agent_2),
        )
        admin.commit()

        ticket_id = ticket_ingest.create_ticket(org_id, source="pdf_upload")
        ticket_ingest.insert_ticket_messages(org_id=org_id, ticket_id=ticket_id, turns=[
            {"seq": 0, "speaker": "customer", "agent_user_id": None,
             "text": "The export button just spins forever."},
            {"seq": 1, "speaker": "agent", "agent_user_id": agent_1,
             "text": "I'll take a look and get back to you."},
            {"seq": 2, "speaker": "customer", "agent_user_id": None,
             "text": "Still spinning an hour later, anyone there?"},
            {"seq": 3, "speaker": "agent", "agent_user_id": agent_2,
             "text": "Sorry for the delay — found a stuck job, fixed and redeployed."},
            {"seq": 4, "speaker": "customer", "agent_user_id": None,
             "text": "Works now, thank you!"},
        ])
        ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT seq, speaker, agent_user_id, text FROM ticket_messages "
                    "WHERE ticket_id = %s ORDER BY seq",
                    (ticket_id,),
                ).fetchall()
        turns = [dict(r) for r in rows]

        # Real psycopg round-trip: agent_user_id comes back as uuid.UUID,
        # not the plain string this test inserted — exactly the type
        # mismatch Cursor's string-only unit tests never exercised.
        real_ids = {t["agent_user_id"] for t in turns if t["agent_user_id"] is not None}
        assert all(isinstance(uid, uuid.UUID) for uid in real_ids)

        spans = ticket_scoring.agent_spans(turns)
        assert len(spans) == 2
        assert str(spans[0]["agent_user_id"]) == agent_1
        assert spans[0]["start_seq"] == 1 and spans[0]["end_seq"] == 2
        assert str(spans[1]["agent_user_id"]) == agent_2
        assert spans[1]["start_seq"] == 3 and spans[1]["end_seq"] == 4

        owner = ticket_scoring.primary_owner(turns)
        assert str(owner) == agent_2  # the resolving agent, not whoever spoke first

        def _dispatch(prompt: str) -> str:
            if "acknowledge" in prompt.lower():
                return json.dumps({
                    "verdict": "pass", "reasoning": "Agent 1 acknowledged promptly.",
                    "evidence_quote": "I'll take a look and get back to you.",
                    "evidence_seq": 1,
                })
            return json.dumps({
                "verdict": "pass", "reasoning": "Agent 2 actually fixed it.",
                "evidence_quote": "found a stuck job, fixed and redeployed.",
                "evidence_seq": 3,
            })

        dims = [
            {"id": "ack", "name": "Acknowledged promptly", "weight": 50,
             "question": "Did an agent acknowledge the issue?"},
            {"id": "fixed", "name": "Actually resolved", "weight": 50,
             "question": "Was the issue actually fixed?"},
        ]
        result = ticket_scoring.score_ticket(turns, dims, call_claude_fn=_dispatch)
        by_id = {f["id"]: f for f in result["findings"]}

        # The whole-thread audit's single v1 owner is Agent 2 (resolver)...
        assert str(result["primary_owner"]) == agent_2
        # ...but per-finding attribution still correctly credits Agent 1
        # for the thing Agent 1 actually did, not Agent 2 by default.
        assert str(by_id["ack"]["attributed_to"]) == agent_1
        assert str(by_id["fixed"]["attributed_to"]) == agent_2
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


# ---------- 2. today's honest limitation: PDF ingestion can't tell two agents apart ----------


def test_real_pdf_ingestion_of_an_agent_bounce_currently_collapses_to_one_span():
    """Documents the known TA-2/TA-3 gap with a real ingested ticket: two
    different named agents ("Tanu" and "Dhruv") in the same PDF both land
    with agent_user_id=NULL, so agent_spans() cannot tell them apart and
    collapses to a single span/primary_owner=None — exactly Cursor's
    documented null-collapses-to-one-identity behavior, now shown against
    a real ingest rather than a hand-built turns list. This is expected,
    not a regression: TA-3's schema is ready for identity resolution
    whenever it exists (see test above), but the PDF parser itself
    doesn't provide it yet (flagged in ticket_ingest.py's docstring)."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import ticket_ingest, ticket_scoring
    from backend.db import connection
    from backend.org_ids import org_scope

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        "SELECT to_regclass('public.ticket_messages') AS m"
    ).fetchone()
    if not exists or not exists["m"]:
        admin.close()
        pytest.skip("0022_tickets not applied")

    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta8-live-test-pdf"))
        admin.commit()

        pdf_bytes = _build_justcall_pdf(
            "Conversation with JustCall\nTicket Details\n"
            "--- August 19, 2026 ---\n"
            "06:16 AM | Kevin Abraham: The export button just spins forever.\n"
            "06:20 AM | Tanu from JustCall: I'll take a look and get back to you.\n"
            "07:00 AM | Kevin Abraham: Still spinning an hour later, anyone there?\n"
            "07:05 AM | Dhruv from JustCall: Sorry for the delay, found a stuck job, fixed it.\n"
            "07:06 AM | Kevin Abraham: Works now, thank you!\n"
            "Exported from JustCall on September 5, 2026 at 03:46 AM"
        )
        ticket_id = ticket_ingest.ingest_ticket_pdf(org_id, pdf_bytes)

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT seq, speaker, agent_user_id, text FROM ticket_messages "
                    "WHERE ticket_id = %s ORDER BY seq",
                    (ticket_id,),
                ).fetchall()
        turns = [dict(r) for r in rows]

        agent_turns = [t for t in turns if t["speaker"] == "agent"]
        assert len(agent_turns) == 2  # Tanu and Dhruv both really are agent turns...
        assert all(t["agent_user_id"] is None for t in agent_turns)  # ...but neither has an id

        spans = ticket_scoring.agent_spans(turns)
        assert len(spans) == 1  # Tanu and Dhruv collapse into one undifferentiated span
        assert spans[0]["agent_user_id"] is None
        assert spans[0]["turn_count"] == 2

        assert ticket_scoring.primary_owner(turns) is None
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def _build_justcall_pdf(text: str) -> bytes:
    """Minimal real one-page PDF (hand-written, no external library)
    whose content stream is exactly the given JustCall-template text."""
    import io

    content = text.replace("(", r"\(").replace(")", r"\)")
    lines = content.split("\n")
    stream_ops = ["BT", "/F1 10 Tf", "72 750 Td", "12 TL"]
    for line in lines:
        stream_ops.append(f"({line}) Tj")
        stream_ops.append("T*")
    stream_ops.append("ET")
    stream = "\n".join(stream_ops).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 1600] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, obj))
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
