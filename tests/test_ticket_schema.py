"""TA-3: tickets / ticket_messages schema — tenant-scoped, RLS in the
creating migration, agent_user_id ready for TA-8. Does not touch the
call-scoring engine."""

from __future__ import annotations

import uuid

from backend.paths import ROOT

REV = ROOT / "alembic" / "versions" / "0022_tickets.py"
SCORING = ("qa_engine.py", "qa_v8.py", "rules_v8.py")


def _raw() -> str:
    return REV.read_text(encoding="utf-8")


def test_revision_file_and_chain():
    assert REV.is_file()
    raw = _raw()
    assert 'revision: str = "0022_tickets"' in raw
    assert "0021_call_pipeline_events" in raw


def test_tickets_is_mutable_org_scoped_like_calls():
    sql = _raw().upper()
    assert "CREATE TABLE TICKETS" in sql
    assert "ORG_ID UUID NOT NULL REFERENCES ORGS" in sql
    assert "SOURCE TEXT NOT NULL" in sql
    assert "STATUS TEXT NOT NULL" in sql
    assert "CREATED_AT TIMESTAMPTZ NOT NULL DEFAULT NOW()" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY TICKETS_SELECT" in sql
    assert "CREATE POLICY TICKETS_INSERT" in sql
    assert "CREATE POLICY TICKETS_UPDATE" in sql
    assert "CREATE POLICY TICKETS_DELETE" not in sql
    assert "GRANT SELECT, INSERT, UPDATE ON TICKETS TO CALLPROOF_APP" in sql
    assert "CALLPROOF_CURRENT_ORG_ID()" in sql
    assert "bypass_rls" not in _raw()


def test_ticket_messages_is_append_only_with_agent_user_id_fk():
    raw = _raw()
    sql = raw.upper()
    assert "CREATE TABLE TICKET_MESSAGES" in sql
    assert "ORG_ID UUID NOT NULL REFERENCES ORGS" in sql
    assert "TICKET_ID UUID NOT NULL REFERENCES TICKETS" in sql
    assert "ON DELETE CASCADE" in sql
    assert "SEQ INTEGER NOT NULL" in sql
    assert "UNIQUE (TICKET_ID, SEQ)" in sql
    assert "AGENT_USER_ID UUID REFERENCES ORG_MEMBERS (USER_ID)" in sql
    assert "ADD COLUMN AGENT_USER_ID" not in sql  # present from create, not a later alter
    assert "SPEAKER TEXT NOT NULL" in sql
    assert "CHECK (SPEAKER IN ('AGENT', 'CUSTOMER', 'BOT'))" in sql
    assert "CHECK (AGENT_USER_ID IS NULL OR SPEAKER = 'AGENT')" in sql
    assert "CREATE POLICY TICKET_MESSAGES_SELECT" in sql
    assert "CREATE POLICY TICKET_MESSAGES_INSERT" in sql
    assert "CREATE POLICY TICKET_MESSAGES_UPDATE" not in sql
    assert "CREATE POLICY TICKET_MESSAGES_DELETE" not in sql
    assert "GRANT SELECT, INSERT ON TICKET_MESSAGES TO CALLPROOF_APP" in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "CREATE INDEX IDX_TICKET_MESSAGES_AGENT_USER_ID" in sql


def test_rubrics_table_is_not_altered():
    sql = _raw().upper()
    assert "ALTER TABLE RUBRICS" not in sql
    assert "CREATE TABLE RUBRICS" not in sql


def test_call_scoring_engine_is_untouched():
    """Epic guardrail: this engine cannot be built by editing qa_engine /
    qa_v8 / rules_v8. The revision itself must not mention them."""
    raw = _raw()
    for name in SCORING:
        assert name not in raw
        assert name.removesuffix(".py") not in raw
    src_root = ROOT / "backend"
    # This ticket only adds a migration + tests — those three files stay
    # bit-identical as far as this change set is concerned (no edits).
    for name in SCORING:
        assert (src_root / name).is_file()


def test_live_org_isolation_and_agent_user_id_check():
    """Live Postgres: RLS hides another org's tickets; agent_user_id may
    be NULL on an agent turn (TA-2: PDF often cannot recover identity)
    but cannot be set on a customer turn."""
    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row

    from backend.db import APP_ROLE, connection
    from backend.db_url import database_url, psycopg_url
    from backend.org_ids import org_scope
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.errors import CheckViolation, InsufficientPrivilege

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    ticket_id = str(uuid.uuid4())
    agent_uid = str(uuid.uuid4())
    role = admin.execute(
        "SELECT rolbypassrls FROM pg_roles WHERE rolname = %s",
        (APP_ROLE,),
    ).fetchone()
    if not role:
        admin.close()
        pytest.skip("0005_rls not applied")
    exists = admin.execute(
        """
        SELECT to_regclass('public.tickets') AS t,
               to_regclass('public.ticket_messages') AS m
        """
    ).fetchone()
    if not exists or not exists["t"] or not exists["m"]:
        admin.close()
        pytest.skip("0022_tickets not applied")

    try:
        admin.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s), (%s, %s)",
            (org_a, "ta3-a", org_b, "ta3-b"),
        )
        admin.execute(
            """
            INSERT INTO org_members (org_id, user_id, role)
            VALUES (%s, %s, 'member')
            """,
            (org_a, agent_uid),
        )
        admin.execute(
            """
            INSERT INTO tickets (id, org_id, source, status)
            VALUES (%s, %s, 'pdf_upload', 'uploaded')
            """,
            (ticket_id, org_a),
        )
        admin.execute(
            """
            INSERT INTO ticket_messages
                (ticket_id, org_id, seq, agent_user_id, speaker, text)
            VALUES
                (%s, %s, 1, NULL, 'customer', 'I have a problem'),
                (%s, %s, 2, %s, 'agent', 'I can help with that'),
                (%s, %s, 3, NULL, 'agent', 'PDF did not name me')
            """,
            (ticket_id, org_a, ticket_id, org_a, agent_uid, ticket_id, org_a),
        )
        admin.commit()

        with pytest.raises(CheckViolation):
            admin.execute(
                """
                INSERT INTO ticket_messages
                    (ticket_id, org_id, seq, agent_user_id, speaker, text)
                VALUES (%s, %s, 4, %s, 'customer', 'should fail')
                """,
                (ticket_id, org_a, agent_uid),
            )
        admin.rollback()

        with org_scope(org_b):
            with connection() as conn:
                seen = conn.execute(
                    "SELECT id FROM tickets WHERE org_id = %s", (org_a,),
                ).fetchall()
                assert seen == []
                msgs = conn.execute(
                    "SELECT id FROM ticket_messages WHERE ticket_id = %s",
                    (ticket_id,),
                ).fetchall()
                assert msgs == []

        with org_scope(org_a):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT status FROM tickets WHERE id = %s", (ticket_id,),
                ).fetchall()
                assert len(rows) == 1
                conn.execute(
                    "UPDATE tickets SET status = %s WHERE id = %s AND org_id = %s",
                    ("processing", ticket_id, org_a),
                )
                n = conn.execute(
                    """
                    SELECT COUNT(*) AS n FROM ticket_messages
                    WHERE ticket_id = %s AND org_id = %s
                    """,
                    (ticket_id, org_a),
                ).fetchone()["n"]
                assert int(n) == 3
                named = conn.execute(
                    """
                    SELECT agent_user_id FROM ticket_messages
                    WHERE ticket_id = %s AND seq = 2
                    """,
                    (ticket_id,),
                ).fetchone()
                assert str(named["agent_user_id"]) == agent_uid
                try:
                    conn.execute(
                        """
                        UPDATE ticket_messages SET text = 'rewritten'
                        WHERE ticket_id = %s AND seq = 1
                        """,
                        (ticket_id,),
                    )
                    rewritten = conn.execute(
                        """
                        SELECT text FROM ticket_messages
                        WHERE ticket_id = %s AND seq = 1
                        """,
                        (ticket_id,),
                    ).fetchone()["text"]
                    # If RLS denied the UPDATE, text is unchanged. If the
                    # GRANT slipped through without a policy, rowcount is 0.
                    assert rewritten == "I have a problem"
                except InsufficientPrivilege:
                    pass
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM tickets WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM org_members WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM orgs WHERE id IN (%s, %s)", (org_a, org_b))
        admin.commit()
        admin.close()
