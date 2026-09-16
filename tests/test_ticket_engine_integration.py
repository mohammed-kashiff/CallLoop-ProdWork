"""Integration proof: TA-4/TA-5 ingestion output feeds TA-6/TA-21 per-agent
scoring with no adapter needed — sequenced {seq, speaker, agent_user_id,
text} turns straight out of the database (PRD §3/§9) are exactly what
score_ticket_for_agent() consumes, once TA-15's alias mapping has resolved
the PDF's raw agent name to a real org_members.user_id (TA-21's per-agent
model has nothing to score without a real identity — see
test_ta8_multi_agent_attribution.py's own documentation of that gap).

Live Postgres + Storage + Claude vision for ingestion (skipped without
those configured, or if 0023_ticket_image_assets isn't applied); scoring
itself uses a fake call_claude_fn so this stays fast and deterministic
and doesn't spend a second, redundant set of real LLM calls scoring
content whose accuracy isn't what this test is checking."""

from __future__ import annotations

import json
import os
import uuid

import pytest
from dotenv import dotenv_values
from PIL import Image

from tests.test_ticket_ingest import _build_justcall_pdf_with_image


def test_a_real_ingested_ticket_scores_correctly_through_ticket_scoring(monkeypatch):
    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    raw_env = dotenv_values(ENV_FILE)
    real_supabase_url = raw_env.get("SUPABASE_URL")
    real_service_role_key = raw_env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not real_supabase_url or not real_service_role_key or "test.supabase.co" in real_supabase_url:
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    # conftest.py stubs SUPABASE_URL for the rest of the suite; scope the
    # real value to just this test (see test_ticket_ingest.py's live
    # image test for the same pattern and why override=True isn't used).
    monkeypatch.setenv("SUPABASE_URL", real_supabase_url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", real_service_role_key)
    for key, value in raw_env.items():
        if key not in os.environ and value is not None:
            monkeypatch.setenv(key, value)

    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

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

    from backend import ticket_image_store
    if not ticket_image_store.configured():
        admin.close()
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")

    from backend import ticket_ingest, ticket_scoring
    from backend.db import connection
    from backend.org_ids import org_scope

    img = Image.new("RGB", (120, 60), (10, 10, 10))
    pdf_bytes = _build_justcall_pdf_with_image(
        "Conversation with JustCall\nTicket Details\n"
        "--- August 19, 2026 ---\n"
        "06:16 AM | Kevin Abraham: Checkout is returning error code 504\n"
        "06:20 AM | Tanu from JustCall: I can see the 504 here, looking into it now\n"
        "Exported from JustCall on September 5, 2026 at 03:46 AM",
        img,
    )

    org_id = str(uuid.uuid4())
    agent_user_id = str(uuid.uuid4())
    ticket_id = None
    asset_seq = None
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta-integration-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'member')",
            (org_id, agent_user_id),
        )
        admin.commit()

        ticket_id = ticket_ingest.ingest_ticket_pdf(org_id, pdf_bytes)

        from backend import ticket_agent_aliases
        ticket_agent_aliases.set_alias(org_id, "Tanu", agent_user_id)

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    """
                    SELECT seq, speaker, agent_user_id, text FROM ticket_messages
                    WHERE ticket_id = %s ORDER BY seq
                    """,
                    (ticket_id,),
                ).fetchall()
        turns = [dict(r) for r in rows]
        assert len(turns) == 3  # 2 text turns + 1 image-derived turn
        image_turn = next(t for t in turns if t["seq"] == 2)
        asset_seq = image_turn["seq"]
        assert image_turn["speaker"] == "agent"  # inherits the preceding turn's speaker
        assert isinstance(image_turn["text"], str) and len(image_turn["text"]) > 0
        # The alias backfill (TA-15) resolved both of Tanu's turns,
        # including the image-derived one — real uuid.UUID round-tripped
        # through Postgres, not the string this test inserted.
        assert str(image_turn["agent_user_id"]) == agent_user_id
        assert ticket_scoring.resolved_agent_ids(turns) == [image_turn["agent_user_id"]]

        # TA-6 consumes exactly what came back from the DB — no reshaping.
        dims = [
            {"id": "diagnosis", "name": "Diagnostic reasoning", "weight": 60,
             "question": "Did the agent correctly identify the error the customer reported?"},
            {"id": "used_evidence", "name": "Used available evidence", "weight": 40,
             "question": "Did the agent reference the screenshot the customer's turn describes?"},
        ]

        def _fake_claude(prompt: str) -> str:
            if "correctly identify" in prompt:
                return json.dumps({
                    "verdict": "pass",
                    "reasoning": "Agent named the 504 the customer reported.",
                    "evidence_quote": "I can see the 504 here, looking into it now",
                    "evidence_seq": 1,
                })
            return json.dumps({
                "verdict": "pass",
                "reasoning": "Agent's screenshot-derived turn is cited as evidence.",
                "evidence_quote": image_turn["text"],
                "evidence_seq": image_turn["seq"],
            })

        result = ticket_scoring.score_ticket_for_agent(
            turns, dims, target_agent_user_id=agent_user_id, call_claude_fn=_fake_claude,
        )

        assert result["agent_user_id"] == agent_user_id
        assert result["score"] == 100.0
        by_id = {f["id"]: f for f in result["findings"]}
        assert by_id["diagnosis"]["evidence_verified"] is True
        # The image-derived turn's own vision text round-trips through
        # verbatim-quote evidence verification like any other turn.
        assert by_id["used_evidence"]["evidence_verified"] is True
        assert by_id["used_evidence"]["evidence_seq"] == image_turn["seq"]
        # Both findings fall inside Tanu's own span, so both attribute to
        # Tanu — content-blind scoring, image included.
        assert str(by_id["diagnosis"]["attributed_to"]) == agent_user_id
        assert str(by_id["used_evidence"]["attributed_to"]) == agent_user_id
    finally:
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_agent_aliases WHERE org_id = %s", (org_id,))
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
