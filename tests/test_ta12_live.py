"""TA-12 (PRD §7) live proof: a real ticket with two real agents, a real
manager, real Postgres round trips through the actual HTTP routes —
GET /api/tickets/{id}, POST .../score, and GET /api/tickets/mine — same
scenario TA-8's live test used, now checked for who is allowed to see
what rather than just attribution correctness."""

from __future__ import annotations

import uuid

import pytest


def test_manager_vs_agent_views_over_a_real_shared_ticket():
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from fastapi.testclient import TestClient
    from psycopg.rows import dict_row

    from backend.api import app
    from backend import ticket_ingest

    exists_conn = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = exists_conn.execute(
        "SELECT to_regclass('public.ticket_messages') AS m"
    ).fetchone()
    if not exists or not exists["m"]:
        exists_conn.close()
        pytest.skip("0022_tickets not applied")

    admin = exists_conn
    org_id = str(uuid.uuid4())
    agent_a = str(uuid.uuid4())
    agent_b = str(uuid.uuid4())
    manager = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta12-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES "
            "(%s, %s, 'member'), (%s, %s, 'member'), (%s, %s, 'owner')",
            (org_id, agent_a, org_id, agent_b, org_id, manager),
        )
        admin.commit()

        ticket_id = ticket_ingest.create_ticket(org_id, source="pdf_upload")
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, [
            {"seq": 0, "speaker": "customer", "agent_user_id": None,
             "text": "The export button just spins forever."},
            {"seq": 1, "speaker": "agent", "agent_user_id": agent_a,
             "text": "I'll take a look and get back to you."},
            {"seq": 2, "speaker": "customer", "agent_user_id": None,
             "text": "Still spinning an hour later."},
            {"seq": 3, "speaker": "agent", "agent_user_id": agent_b,
             "text": "Found a stuck job, fixed and redeployed."},
            {"seq": 4, "speaker": "customer", "agent_user_id": None,
             "text": "Works now, thank you!"},
        ])
        ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")

        from tests.conftest import mint_access_token
        from backend.auth import Membership

        def _client_as(user_id: str, role: str, monkeypatch) -> TestClient:
            import backend.auth as auth_mod
            monkeypatch.setattr(
                auth_mod, "ensure_membership",
                lambda uid, email=None, first_name=None, last_name=None: Membership(
                    org_id, role, str(uid),
                ),
            )
            c = TestClient(app)
            c.headers["Authorization"] = f"Bearer {mint_access_token(sub=user_id)}"
            return c

        import _pytest.monkeypatch as mp_mod
        mp = mp_mod.MonkeyPatch()
        try:
            # Manager sees the whole real thread.
            manager_client = _client_as(manager, "owner", mp)
            r = manager_client.get(f"/api/tickets/{ticket_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["view_scope"] == "full"
            assert len(body["messages"]) == 5

            # Agent A sees only their own span of the real thread.
            agent_a_client = _client_as(agent_a, "member", mp)
            r = agent_a_client.get(f"/api/tickets/{ticket_id}")
            assert r.status_code == 200
            body = r.json()
            assert body["view_scope"] == "own"
            assert [m["seq"] for m in body["messages"]] == [1, 2]

            # Agent B sees only their own span — never agent A's turns.
            agent_b_client = _client_as(agent_b, "member", mp)
            r = agent_b_client.get(f"/api/tickets/{ticket_id}")
            assert r.status_code == 200
            body = r.json()
            assert [m["seq"] for m in body["messages"]] == [3, 4]

            # Agent A's cross-ticket rollup finds this ticket and only
            # their own turns on it.
            r = agent_a_client.get("/api/tickets/mine")
            assert r.status_code == 200
            mine = r.json()["tickets"]
            assert len(mine) == 1
            assert mine[0]["ticket_id"] == ticket_id
            assert [t["seq"] for t in mine[0]["turns"]] == [1, 2]

            # Agent B's rollup does NOT include agent A's turns on the
            # same shared ticket.
            r = agent_b_client.get("/api/tickets/mine")
            assert r.status_code == 200
            mine_b = r.json()["tickets"]
            assert len(mine_b) == 1
            assert [t["seq"] for t in mine_b[0]["turns"]] == [3, 4]
        finally:
            mp.undo()
    finally:
        admin.execute("DELETE FROM ticket_audits WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_message_assets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
