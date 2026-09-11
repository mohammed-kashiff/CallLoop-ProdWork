"""TA-15: org owner-managed mapping from a ticket PDF's raw agent display
name to a real org_members.user_id.

Fake-connection unit tests for backend/ticket_agent_aliases.py's CRUD/
resolution functions, HTTP-layer tests for backend/ticket_agent_aliases_api.py
(owner-only enforcement is the load-bearing behavior there), plus a live
Postgres test (skipped without DATABASE_URL, or if 0025 isn't applied) that
proves a real mapping resolves end to end through ingest_ticket_pdf() and
that TA-8's own attribution mechanism then has something to work with."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token

ORG_A = str(uuid.uuid4())
U1 = str(uuid.uuid4())
U2 = str(uuid.uuid4())
U3 = str(uuid.uuid4())
OLD_USER = str(uuid.uuid4())
NEW_USER = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self):
        self.aliases: dict[tuple, dict] = {}
        self.messages: list[dict] = []
        self.members: list[dict] = []
        self.executed: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.executed.append((norm, params))
        if norm.startswith("SELECT DISPLAY_NAME, USER_ID, CREATED_AT, UPDATED_AT"):
            (org_id,) = params
            rows = [
                {"display_name": k[1], "user_id": v["user_id"],
                 "created_at": None, "updated_at": None}
                for k, v in self.aliases.items() if k[0] == org_id
            ]
            return _Result(sorted(rows, key=lambda r: r["display_name"]))
        if norm.startswith("SELECT SPEAKER_DISPLAY_NAME, COUNT(*) AS TURN_COUNT"):
            (org_id,) = params
            counts: dict[str, int] = {}
            for m in self.messages:
                if m["org_id"] != org_id or m["speaker"] != "agent":
                    continue
                if m["agent_user_id"] is not None or not m["speaker_display_name"]:
                    continue
                counts[m["speaker_display_name"]] = counts.get(m["speaker_display_name"], 0) + 1
            rows = [{"speaker_display_name": k, "turn_count": v} for k, v in counts.items()]
            return _Result(sorted(rows, key=lambda r: (-r["turn_count"], r["speaker_display_name"])))
        if norm.startswith("INSERT INTO TICKET_AGENT_ALIASES"):
            org_id, name, uid = params
            self.aliases[(org_id, name)] = {"user_id": uid}
            return _Result([])
        if norm.startswith("DELETE FROM TICKET_AGENT_ALIASES"):
            org_id, name = params
            self.aliases.pop((org_id, name), None)
            return _Result([])
        if norm.startswith("SELECT USER_ID FROM TICKET_AGENT_ALIASES WHERE ORG_ID = %S AND DISPLAY_NAME = %S"):
            org_id, name = params
            row = self.aliases.get((org_id, name))
            return _Result([{"user_id": row["user_id"]}] if row else [])
        if norm.startswith("SELECT DISPLAY_NAME, USER_ID FROM TICKET_AGENT_ALIASES"):
            org_id, names = params
            rows = [
                {"display_name": k[1], "user_id": v["user_id"]}
                for k, v in self.aliases.items() if k[0] == org_id and k[1] in names
            ]
            return _Result(rows)
        if norm.startswith("SELECT USER_ID, FIRST_NAME, LAST_NAME, ROLE"):
            (org_id,) = params
            rows = [m for m in self.members if m["org_id"] == org_id]
            return _Result(sorted(rows, key=lambda r: (r["first_name"] or "", r["last_name"] or "")))
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.ticket_agent_aliases.db.connection", _cm)
    monkeypatch.setattr("backend.ticket_agent_aliases.org_scope", lambda oid: _cm())
    yield conn


# ---------- set_alias / list_aliases / delete_alias ----------


def test_set_alias_creates_a_new_mapping(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        result = ticket_agent_aliases.set_alias(ORG_A, "Kashif", U1)
    assert result == {"display_name": "Kashif", "user_id": U1}
    assert conn.aliases[(ORG_A, "Kashif")] == {"user_id": U1}


def test_set_alias_strips_the_display_name(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        ticket_agent_aliases.set_alias(ORG_A, "  Kashif  ", U1)
    assert (ORG_A, "Kashif") in conn.aliases


def test_set_alias_rejects_blank_display_name(monkeypatch):
    from backend import ticket_agent_aliases
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            ticket_agent_aliases.set_alias(ORG_A, "   ", U1)
    assert exc.value.status_code == 400


def test_set_alias_rejects_invalid_user_id(monkeypatch):
    from backend import ticket_agent_aliases
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            ticket_agent_aliases.set_alias(ORG_A, "Kashif", "not-a-uuid")
    assert exc.value.status_code == 400


def test_set_alias_updates_an_existing_mapping(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    conn.aliases[(ORG_A, "Kashif")] = {"user_id": OLD_USER}
    with _fake_db(monkeypatch, conn):
        ticket_agent_aliases.set_alias(ORG_A, "Kashif", NEW_USER)
    assert conn.aliases[(ORG_A, "Kashif")] == {"user_id": NEW_USER}


def test_list_aliases_returns_only_this_orgs_mappings(monkeypatch):
    from backend import ticket_agent_aliases

    other_org = str(uuid.uuid4())
    conn = _FakeConn()
    conn.aliases[(ORG_A, "Kashif")] = {"user_id": "u1"}
    conn.aliases[(ORG_A, "Tanu")] = {"user_id": "u2"}
    conn.aliases[(other_org, "Someone")] = {"user_id": "u3"}
    with _fake_db(monkeypatch, conn):
        result = ticket_agent_aliases.list_aliases(ORG_A)
    assert {r["display_name"] for r in result} == {"Kashif", "Tanu"}


def test_delete_alias_removes_the_mapping(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    conn.aliases[(ORG_A, "Kashif")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        ticket_agent_aliases.delete_alias(ORG_A, "Kashif")
    assert (ORG_A, "Kashif") not in conn.aliases


def test_delete_alias_rejects_blank_display_name(monkeypatch):
    from backend import ticket_agent_aliases
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            ticket_agent_aliases.delete_alias(ORG_A, "  ")
    assert exc.value.status_code == 400


# ---------- resolve_agent_user_id / resolve_agent_user_ids ----------


def test_resolve_agent_user_id_returns_the_mapped_id(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    conn.aliases[(ORG_A, "Kashif")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        assert ticket_agent_aliases.resolve_agent_user_id(ORG_A, "Kashif") == "u1"


def test_resolve_agent_user_id_returns_none_when_unmapped(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert ticket_agent_aliases.resolve_agent_user_id(ORG_A, "Nobody") is None


def test_resolve_agent_user_id_returns_none_for_blank_name(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert ticket_agent_aliases.resolve_agent_user_id(ORG_A, None) is None
        assert ticket_agent_aliases.resolve_agent_user_id(ORG_A, "  ") is None
    assert conn.executed == []  # never even queries for a blank name


def test_resolve_agent_user_ids_batch_returns_only_mapped_names(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    conn.aliases[(ORG_A, "Kashif")] = {"user_id": "u1"}
    conn.aliases[(ORG_A, "Tanu")] = {"user_id": "u2"}
    with _fake_db(monkeypatch, conn):
        result = ticket_agent_aliases.resolve_agent_user_ids(ORG_A, {"Kashif", "Someone Else"})
    assert result == {"Kashif": "u1"}


def test_resolve_agent_user_ids_empty_input_short_circuits(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert ticket_agent_aliases.resolve_agent_user_ids(ORG_A, set()) == {}
    assert conn.executed == []  # never queries for an empty set


# ---------- list_unresolved_agent_names ----------


def test_list_unresolved_agent_names_counts_agent_turns_with_no_resolved_id(monkeypatch):
    from backend import ticket_agent_aliases

    conn = _FakeConn()
    conn.messages = [
        {"org_id": ORG_A, "speaker": "agent", "agent_user_id": None, "speaker_display_name": "Kashif"},
        {"org_id": ORG_A, "speaker": "agent", "agent_user_id": None, "speaker_display_name": "Kashif"},
        {"org_id": ORG_A, "speaker": "agent", "agent_user_id": "u1", "speaker_display_name": "Tanu"},
        # A customer turn now carries a real name too (speaker_display_name
        # is no longer agent-only) — must still be excluded by speaker != 'agent',
        # not by this column being empty.
        {"org_id": ORG_A, "speaker": "customer", "agent_user_id": None, "speaker_display_name": "Kevin"},
    ]
    with _fake_db(monkeypatch, conn):
        result = ticket_agent_aliases.list_unresolved_agent_names(ORG_A)
    assert result == [{"display_name": "Kashif", "turn_count": 2}]


# ---------- list_org_agents ----------


def test_list_org_agents_returns_this_orgs_members(monkeypatch):
    from backend import ticket_agent_aliases

    other_org = str(uuid.uuid4())
    conn = _FakeConn()
    conn.members = [
        {"org_id": ORG_A, "user_id": "u1", "first_name": "Kashif", "last_name": "M", "role": "owner"},
        {"org_id": ORG_A, "user_id": "u2", "first_name": "Tanu", "last_name": "S", "role": "member"},
        {"org_id": other_org, "user_id": "u3", "first_name": "Other", "last_name": "Org", "role": "owner"},
    ]
    with _fake_db(monkeypatch, conn):
        result = ticket_agent_aliases.list_org_agents(ORG_A)
    assert {r["user_id"] for r in result} == {"u1", "u2"}


def test_module_never_bypasses_rls():
    src = (ROOT / "backend" / "ticket_agent_aliases.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src


# ---------- HTTP layer: /api/tickets/agent-aliases ----------


def test_list_agent_aliases_requires_auth():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/tickets/agent-aliases")
    assert r.status_code == 401


def test_list_agent_aliases_is_owner_only(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from backend.auth import Membership

    client = TestClient(app)
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id)
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.get("/api/tickets/agent-aliases")
    assert r.status_code == 403


def test_list_agent_aliases_returns_aliases_unresolved_and_members(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)  # conftest's authorize() is role="owner"
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_aliases",
        lambda org_id: [{"display_name": "Kashif", "user_id": "u1",
                          "created_at": None, "updated_at": None}],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_unresolved_agent_names",
        lambda org_id: [{"display_name": "Tanu", "turn_count": 3}],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_org_agents",
        lambda org_id: [{"user_id": "u1", "first_name": "Kashif", "last_name": "M", "role": "owner"}],
    )
    r = client.get("/api/tickets/agent-aliases")
    assert r.status_code == 200
    body = r.json()
    assert body["aliases"][0]["display_name"] == "Kashif"
    assert body["unresolved"][0]["display_name"] == "Tanu"
    assert body["members"][0]["user_id"] == "u1"


def test_set_agent_alias_requires_owner(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from backend.auth import Membership

    client = TestClient(app)
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id)
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.post("/api/tickets/agent-aliases", json={"display_name": "Tanu", "user_id": str(uuid.uuid4())})
    assert r.status_code == 403


def test_set_agent_alias_success(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.set_alias",
        lambda org_id, name, uid: calls.append((org_id, name, uid)) or {"display_name": name, "user_id": uid},
    )
    target = str(uuid.uuid4())
    r = client.post("/api/tickets/agent-aliases", json={"display_name": "Tanu", "user_id": target})
    assert r.status_code == 200
    assert r.json() == {"display_name": "Tanu", "user_id": target}
    assert calls[0][1:] == ("Tanu", target)


def test_delete_agent_alias_requires_owner(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from backend.auth import Membership

    client = TestClient(app)
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id)
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.delete("/api/tickets/agent-aliases/Tanu")
    assert r.status_code == 403


def test_delete_agent_alias_success(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.delete_alias",
        lambda org_id, name: calls.append((org_id, name)),
    )
    r = client.delete("/api/tickets/agent-aliases/Tanu")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls[0][1] == "Tanu"


def test_agent_aliases_route_is_not_swallowed_by_ticket_id_route(monkeypatch):
    """Regression guard: /api/tickets/agent-aliases must be registered
    before /api/tickets/{ticket_id} — otherwise FastAPI's first-match
    routing treats "agent-aliases" as a ticket_id and this 404s/400s on
    ticket_api.get_ticket instead of ever reaching this module."""
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_aliases", lambda org_id: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_unresolved_agent_names",
        lambda org_id: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_aliases_api.ticket_agent_aliases.list_org_agents", lambda org_id: [],
    )
    r = client.get("/api/tickets/agent-aliases")
    assert r.status_code == 200
    assert r.json() == {"aliases": [], "unresolved": [], "members": []}


# ---------- live Postgres: real end-to-end ----------


def test_alias_resolution_live_end_to_end():
    """Real Postgres, own temp org: configure a mapping, ingest a PDF whose
    agent turns carry that raw name, and prove agent_user_id resolves —
    the actual TA-15 success condition — and that TA-8's agent_spans()
    then has more than one span to work with instead of collapsing every
    bounce into one undifferentiated span."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import ticket_agent_aliases, ticket_ingest, ticket_scoring
    from backend.org_ids import org_scope
    from backend.db import connection

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        "SELECT to_regclass('public.ticket_agent_aliases') AS a"
    ).fetchone()
    if not exists or not exists["a"]:
        admin.close()
        pytest.skip("0025_ticket_agent_aliases not applied")

    org_id = str(uuid.uuid4())
    agent_kashif = str(uuid.uuid4())
    agent_tanu = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ta15-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner'), (%s, %s, 'member')",
            (org_id, agent_kashif, org_id, agent_tanu),
        )
        admin.commit()

        assert ticket_agent_aliases.resolve_agent_user_id(org_id, "Kashif") is None
        ticket_agent_aliases.set_alias(org_id, "Kashif", agent_kashif)
        ticket_agent_aliases.set_alias(org_id, "Tanu", agent_tanu)
        assert {a["display_name"] for a in ticket_agent_aliases.list_aliases(org_id)} == {"Kashif", "Tanu"}

        turns = [
            {"seq": 0, "speaker": "customer", "speaker_name": "Kevin",
             "agent_user_id": None, "text": "It's broken again"},
            {"seq": 1, "speaker": "agent", "speaker_name": "Kashif",
             "agent_user_id": None, "text": "Looking into it now"},
            {"seq": 2, "speaker": "customer", "speaker_name": "Kevin",
             "agent_user_id": None, "text": "Still broken"},
            {"seq": 3, "speaker": "agent", "speaker_name": "Tanu",
             "agent_user_id": None, "text": "Picking this up, fixed it"},
        ]
        ticket_id = ticket_ingest.create_ticket(org_id, source="pdf_upload")
        unresolved = {t["speaker_name"] for t in turns if t["speaker"] == "agent"}
        resolved = ticket_agent_aliases.resolve_agent_user_ids(org_id, unresolved)
        for t in turns:
            if t["speaker"] == "agent":
                t["agent_user_id"] = resolved.get(t["speaker_name"])
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
        ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT seq, agent_user_id, speaker_display_name FROM ticket_messages "
                    "WHERE ticket_id = %s ORDER BY seq",
                    (ticket_id,),
                ).fetchall()
        assert rows[1]["agent_user_id"] == uuid.UUID(agent_kashif)
        assert rows[1]["speaker_display_name"] == "Kashif"
        assert rows[3]["agent_user_id"] == uuid.UUID(agent_tanu)
        assert rows[3]["speaker_display_name"] == "Tanu"
        # Customer turns now carry their own name too, not just agents.
        assert rows[0]["speaker_display_name"] == "Kevin"
        assert rows[2]["speaker_display_name"] == "Kevin"

        # TA-8's own attribution mechanism now has two real spans, not one
        # undifferentiated span across both agents — the actual gap TA-15
        # was opened to close.
        formatted = [
            {"seq": r["seq"], "speaker": "agent" if r["agent_user_id"] else "customer",
             "agent_user_id": str(r["agent_user_id"]) if r["agent_user_id"] else None,
             "text": ""}
            for r in rows
        ]
        spans = ticket_scoring.agent_spans(formatted)
        assert {s["agent_user_id"] for s in spans} == {agent_kashif, agent_tanu}

        # Removing the mapping doesn't retroactively touch already-ingested
        # turns (a known, accepted v1 boundary) — only future ingestions.
        ticket_agent_aliases.delete_alias(org_id, "Kashif")
        assert ticket_agent_aliases.resolve_agent_user_id(org_id, "Kashif") is None
        with org_scope(org_id):
            with connection() as conn:
                still_there = conn.execute(
                    "SELECT agent_user_id FROM ticket_messages WHERE ticket_id = %s AND seq = 1",
                    (ticket_id,),
                ).fetchone()
        assert still_there["agent_user_id"] == uuid.UUID(agent_kashif)

        unresolved_names = ticket_agent_aliases.list_unresolved_agent_names(org_id)
        assert unresolved_names == []  # already-resolved turns don't show as unresolved

        members = ticket_agent_aliases.list_org_agents(org_id)
        assert {m["user_id"] for m in members} == {agent_kashif, agent_tanu}
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_agent_aliases WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
