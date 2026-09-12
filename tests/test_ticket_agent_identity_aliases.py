"""IN-10: org owner-managed mapping from a provider's structured agent
identifier (Intercom's author.email) to a real org_members.user_id.

Fake-connection unit tests for backend/ticket_agent_identity_aliases.py's
CRUD/resolution functions, HTTP-layer tests for
backend/ticket_agent_identity_aliases_api.py (owner-only enforcement is
the load-bearing behavior there), plus a live Postgres test (skipped
without DATABASE_URL, or if 0034 isn't applied) that proves a real
mapping resolves end to end through intercom_ingest's own resolution
step and that TA-8's attribution mechanism then has something to work
with — the same proof shape test_ticket_agent_aliases.py already uses
for the PDF path."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token

ORG_A = str(uuid.uuid4())
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
        self.executed: list[tuple] = []
        # Flat rows, joins already done — same simplification style
        # test_ticket_agent_aliases.py's own fake uses for ticket_messages.
        self.messages: list[dict] = []
        self.org_directory: list[dict] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.executed.append((norm, params))
        if norm.startswith("SELECT PROVIDER, IDENTIFIER, USER_ID, CREATED_AT, UPDATED_AT"):
            if "AND PROVIDER = %S" in norm:
                org_id, provider = params
                rows = [
                    {"provider": k[1], "identifier": k[2], "user_id": v["user_id"],
                     "created_at": None, "updated_at": None}
                    for k, v in self.aliases.items() if k[0] == org_id and k[1] == provider
                ]
            else:
                (org_id,) = params
                rows = [
                    {"provider": k[1], "identifier": k[2], "user_id": v["user_id"],
                     "created_at": None, "updated_at": None}
                    for k, v in self.aliases.items() if k[0] == org_id
                ]
            return _Result(sorted(rows, key=lambda r: (r["provider"], r["identifier"])))
        if norm.startswith("INSERT INTO TICKET_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier, uid = params
            self.aliases[(org_id, provider, identifier)] = {"user_id": uid}
            return _Result([])
        if norm.startswith("DELETE FROM TICKET_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier = params
            self.aliases.pop((org_id, provider, identifier), None)
            return _Result([])
        if norm.startswith("SELECT USER_ID FROM TICKET_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier = params
            row = self.aliases.get((org_id, provider, identifier))
            return _Result([{"user_id": row["user_id"]}] if row else [])
        if norm.startswith("SELECT IDENTIFIER, USER_ID FROM TICKET_AGENT_IDENTITY_ALIASES"):
            org_id, provider, idents = params
            rows = [
                {"identifier": k[2], "user_id": v["user_id"]}
                for k, v in self.aliases.items()
                if k[0] == org_id and k[1] == provider and k[2] in idents
            ]
            return _Result(rows)
        if norm.startswith("SELECT M.SPEAKER_DISPLAY_NAME, COUNT(*) AS TURN_COUNT"):
            org_id, source = params
            counts: dict[str, int] = {}
            for m_ in self.messages:
                if (
                    m_["org_id"] != org_id or m_["source"] != source
                    or m_["speaker"] != "agent" or m_["agent_user_id"] is not None
                    or not m_["speaker_display_name"]
                ):
                    continue
                counts[m_["speaker_display_name"]] = counts.get(m_["speaker_display_name"], 0) + 1
            rows = [{"speaker_display_name": k, "turn_count": v} for k, v in counts.items()]
            return _Result(sorted(rows, key=lambda r: (-r["turn_count"], r["speaker_display_name"])))
        if norm.startswith("SELECT USER_ID, EMAIL, FIRST_NAME, LAST_NAME FROM ORG_DIRECTORY"):
            org_id, idents = params
            rows = [
                r for r in self.org_directory
                if r["org_id"] == org_id and r["email"].lower() in idents
            ]
            return _Result(rows)
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.ticket_agent_identity_aliases.db.connection", _cm)
    monkeypatch.setattr("backend.ticket_agent_identity_aliases.org_scope", lambda oid: _cm())
    yield conn


# ---------- set_alias / list_aliases / delete_alias ----------


def test_set_alias_creates_a_new_mapping(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        result = m.set_alias(ORG_A, "intercom", "Kashif@Intercom.example", OLD_USER)
    assert result == {"provider": "intercom", "identifier": "kashif@intercom.example", "user_id": OLD_USER}
    assert conn.aliases[(ORG_A, "intercom", "kashif@intercom.example")] == {"user_id": OLD_USER}


def test_set_alias_normalizes_case_and_whitespace(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        m.set_alias(ORG_A, "  Intercom  ", "  Kashif@Intercom.example  ", OLD_USER)
    assert (ORG_A, "intercom", "kashif@intercom.example") in conn.aliases


def test_set_alias_rejects_blank_identifier(monkeypatch):
    from backend import ticket_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.set_alias(ORG_A, "intercom", "   ", OLD_USER)
    assert exc.value.status_code == 400


def test_set_alias_rejects_blank_provider(monkeypatch):
    from backend import ticket_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.set_alias(ORG_A, "  ", "kashif@intercom.example", OLD_USER)
    assert exc.value.status_code == 400


def test_set_alias_rejects_invalid_user_id(monkeypatch):
    from backend import ticket_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.set_alias(ORG_A, "intercom", "kashif@intercom.example", "not-a-uuid")
    assert exc.value.status_code == 400


def test_set_alias_updates_an_existing_mapping(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "kashif@intercom.example")] = {"user_id": OLD_USER}
    with _fake_db(monkeypatch, conn):
        m.set_alias(ORG_A, "intercom", "kashif@intercom.example", NEW_USER)
    assert conn.aliases[(ORG_A, "intercom", "kashif@intercom.example")] == {"user_id": NEW_USER}


def test_list_aliases_returns_only_this_orgs_mappings(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    other_org = str(uuid.uuid4())
    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "a@b.com")] = {"user_id": "u1"}
    conn.aliases[(ORG_A, "intercom", "c@d.com")] = {"user_id": "u2"}
    conn.aliases[(other_org, "intercom", "e@f.com")] = {"user_id": "u3"}
    with _fake_db(monkeypatch, conn):
        result = m.list_aliases(ORG_A)
    assert {r["identifier"] for r in result} == {"a@b.com", "c@d.com"}


def test_list_aliases_filters_by_provider_when_given(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "a@b.com")] = {"user_id": "u1"}
    conn.aliases[(ORG_A, "zendesk", "c@d.com")] = {"user_id": "u2"}
    with _fake_db(monkeypatch, conn):
        result = m.list_aliases(ORG_A, provider="intercom")
    assert {r["identifier"] for r in result} == {"a@b.com"}


def test_delete_alias_removes_the_mapping(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "a@b.com")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        m.delete_alias(ORG_A, "intercom", "a@b.com")
    assert (ORG_A, "intercom", "a@b.com") not in conn.aliases


def test_delete_alias_rejects_blank_identifier(monkeypatch):
    from backend import ticket_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.delete_alias(ORG_A, "intercom", "  ")
    assert exc.value.status_code == 400


# ---------- resolve_agent_user_id / resolve_agent_user_ids ----------


def test_resolve_agent_user_id_returns_the_mapped_id(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "a@b.com")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "intercom", "A@B.com") == "u1"  # case-insensitive


def test_resolve_agent_user_id_returns_none_when_unmapped(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "intercom", "nobody@b.com") is None


def test_resolve_agent_user_id_returns_none_for_blank_identifier(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "intercom", None) is None
        assert m.resolve_agent_user_id(ORG_A, "intercom", "  ") is None
    assert conn.executed == []  # never even queries for a blank identifier


def test_resolve_agent_user_ids_batch_returns_only_mapped_identifiers(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "intercom", "a@b.com")] = {"user_id": "u1"}
    conn.aliases[(ORG_A, "intercom", "c@d.com")] = {"user_id": "u2"}
    with _fake_db(monkeypatch, conn):
        result = m.resolve_agent_user_ids(ORG_A, "intercom", {"A@B.com", "nobody@e.com"})
    assert result == {"a@b.com": "u1"}


def test_resolve_agent_user_ids_empty_input_short_circuits(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_ids(ORG_A, "intercom", set()) == {}
    assert conn.executed == []


# ---------- list_unresolved_identifiers / suggest_identity_matches ----------


def test_list_unresolved_identifiers_counts_agent_turns_with_no_resolved_id(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.messages = [
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "agent",
         "agent_user_id": None, "speaker_display_name": "kashif@cloop.example"},
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "agent",
         "agent_user_id": None, "speaker_display_name": "kashif@cloop.example"},
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "agent",
         "agent_user_id": "u1", "speaker_display_name": "tanu@cloop.example"},
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "customer",
         "agent_user_id": None, "speaker_display_name": "kevin@customer.example"},
        {"org_id": ORG_A, "source": "pdf_upload", "speaker": "agent",
         "agent_user_id": None, "speaker_display_name": "Kashif"},
    ]
    with _fake_db(monkeypatch, conn):
        result = m.list_unresolved_identifiers(ORG_A, "intercom")
    assert result == [{"identifier": "kashif@cloop.example", "turn_count": 2}]


def test_suggest_identity_matches_pairs_by_exact_email(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    conn.messages = [
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "agent",
         "agent_user_id": None, "speaker_display_name": "kashif@cloop.example"},
        {"org_id": ORG_A, "source": "intercom_api", "speaker": "agent",
         "agent_user_id": None, "speaker_display_name": "stranger@nowhere.example"},
    ]
    conn.org_directory = [
        {"org_id": ORG_A, "user_id": "u1", "email": "Kashif@Cloop.example",
         "first_name": "Kashif", "last_name": "K"},
    ]
    with _fake_db(monkeypatch, conn):
        result = m.suggest_identity_matches(ORG_A, "intercom")
    by_ident = {r["identifier"]: r for r in result}
    assert by_ident["kashif@cloop.example"]["suggested_user_id"] == "u1"
    assert by_ident["kashif@cloop.example"]["suggested_name"] == "Kashif K"
    assert by_ident["stranger@nowhere.example"]["suggested_user_id"] is None
    assert by_ident["stranger@nowhere.example"]["suggested_name"] is None


def test_suggest_identity_matches_empty_when_nothing_unresolved(monkeypatch):
    from backend import ticket_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.suggest_identity_matches(ORG_A, "intercom") == []
    # Never queries org_directory at all when there's nothing to match.
    assert not any("ORG_DIRECTORY" in norm for norm, _ in conn.executed)


def test_module_only_bypasses_rls_for_the_org_directory_suggestion_lookup():
    """org_directory is deliberately REVOKEd from callproof_app (see
    0011_org_members_names_and_directory_view.py's own comment) — reading
    it for suggest_identity_matches() needs bypass_rls, the one narrow,
    justified exception in this file. Every other function here must
    stay on the normal, RLS-respecting connection. Isolate the one
    function's own source rather than a whole-file substring check, so
    a bypass_rls creeping into any OTHER function still fails this."""
    src = (ROOT / "backend" / "ticket_agent_identity_aliases.py").read_text(encoding="utf-8")
    before, _, rest = src.partition("def suggest_identity_matches")
    fn_src = "def suggest_identity_matches" + rest.split("\ndef ")[0]
    assert "bypass_rls=True" not in before
    assert "bypass_rls=True" in fn_src
    assert "WHERE org_id = %s" in fn_src


# ---------- HTTP layer: /api/tickets/agent-identity-aliases ----------


def test_list_agent_identity_aliases_requires_auth():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/tickets/agent-identity-aliases")
    assert r.status_code == 401


def test_list_agent_identity_aliases_is_owner_only(monkeypatch):
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
    r = client.get("/api/tickets/agent-identity-aliases")
    assert r.status_code == 403


def test_list_agent_identity_aliases_returns_aliases(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.list_aliases",
        lambda org_id: [{"provider": "intercom", "identifier": "a@b.com", "user_id": "u1",
                          "created_at": None, "updated_at": None}],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases"
        ".suggest_identity_matches",
        lambda org_id, provider: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_aliases.list_org_agents",
        lambda org_id: [],
    )
    r = client.get("/api/tickets/agent-identity-aliases")
    assert r.status_code == 200
    assert r.json()["aliases"][0]["identifier"] == "a@b.com"


def test_list_agent_identity_aliases_returns_suggested_matches_and_roster(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.list_aliases",
        lambda org_id: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases"
        ".suggest_identity_matches",
        lambda org_id, provider: [
            {"identifier": "kashif@cloop.example", "turn_count": 3,
             "suggested_user_id": "u1", "suggested_name": "Kashif K"},
        ],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_aliases.list_org_agents",
        lambda org_id: [{"user_id": "u1", "first_name": "Kashif", "last_name": "K", "role": "owner"}],
    )
    r = client.get("/api/tickets/agent-identity-aliases")
    assert r.status_code == 200
    body = r.json()
    assert body["unresolved"][0]["suggested_user_id"] == "u1"
    assert body["members"][0]["user_id"] == "u1"


def test_set_agent_identity_alias_requires_owner(monkeypatch):
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
    r = client.post(
        "/api/tickets/agent-identity-aliases",
        json={"provider": "intercom", "identifier": "a@b.com", "user_id": str(uuid.uuid4())},
    )
    assert r.status_code == 403


def test_set_agent_identity_alias_success(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.set_alias",
        lambda org_id, provider, identifier, uid: calls.append((org_id, provider, identifier, uid))
        or {"provider": provider, "identifier": identifier, "user_id": uid},
    )
    target = str(uuid.uuid4())
    r = client.post(
        "/api/tickets/agent-identity-aliases",
        json={"provider": "intercom", "identifier": "a@b.com", "user_id": target},
    )
    assert r.status_code == 200
    assert r.json() == {"provider": "intercom", "identifier": "a@b.com", "user_id": target}
    assert calls[0][1:] == ("intercom", "a@b.com", target)


def test_set_agent_identity_alias_defaults_provider_to_intercom(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.set_alias",
        lambda org_id, provider, identifier, uid: calls.append(provider)
        or {"provider": provider, "identifier": identifier, "user_id": uid},
    )
    r = client.post(
        "/api/tickets/agent-identity-aliases",
        json={"identifier": "a@b.com", "user_id": str(uuid.uuid4())},
    )
    assert r.status_code == 200
    assert calls[0] == "intercom"


def test_delete_agent_identity_alias_requires_owner(monkeypatch):
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
    r = client.delete("/api/tickets/agent-identity-aliases/intercom/a@b.com")
    assert r.status_code == 403


def test_delete_agent_identity_alias_success(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.delete_alias",
        lambda org_id, provider, identifier: calls.append((provider, identifier)),
    )
    r = client.delete("/api/tickets/agent-identity-aliases/intercom/a@b.com")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert calls[0] == ("intercom", "a@b.com")


def test_agent_identity_aliases_route_is_not_swallowed_by_ticket_id_route(monkeypatch):
    """Regression guard: /api/tickets/agent-identity-aliases must be
    registered before /api/tickets/{ticket_id} — same concern
    test_ticket_agent_aliases.py already guards for its own route."""
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases.list_aliases",
        lambda org_id: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_identity_aliases"
        ".suggest_identity_matches",
        lambda org_id, provider: [],
    )
    monkeypatch.setattr(
        "backend.ticket_agent_identity_aliases_api.ticket_agent_aliases.list_org_agents",
        lambda org_id: [],
    )
    r = client.get("/api/tickets/agent-identity-aliases")
    assert r.status_code == 200
    assert r.json() == {"aliases": [], "unresolved": [], "members": []}


# ---------- live Postgres: real end-to-end ----------


def test_identity_alias_resolution_live_end_to_end():
    """Real Postgres, own temp org: configure an email mapping, run a
    real Intercom conversation through intercom_ingest, and prove
    agent_user_id resolves via author.email — the actual IN-10 success
    condition — and that TA-8's agent_spans() then has real spans to
    work with instead of every agent turn collapsing to unattributed."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import (
        intercom_ingest, ticket_agent_identity_aliases as m,
        ticket_ingest, ticket_scoring,
    )
    from backend.db import connection
    from backend.org_ids import org_scope

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute(
        "SELECT to_regclass('public.ticket_agent_identity_aliases') AS a"
    ).fetchone()
    if not exists or not exists["a"]:
        admin.close()
        pytest.skip("0034_ticket_agent_id_alias not applied")

    org_id = str(uuid.uuid4())
    agent_kashif = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "in10-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, agent_kashif),
        )
        admin.commit()

        assert m.resolve_agent_user_id(org_id, "intercom", "kashif@intercom.example") is None
        m.set_alias(org_id, "intercom", "kashif@intercom.example", agent_kashif)

        conversation = {
            "created_at": 1788900000,
            "source": {"body": "<p>Help please</p>", "author": {"type": "user", "email": "a@b.com"}},
            "conversation_parts": {"conversation_parts": [
                {"part_type": "comment", "created_at": 1788900100,
                 "body": "<p>On it</p>",
                 "author": {"type": "admin", "email": "kashif@intercom.example"}},
            ]},
        }
        # Exercise the real resolution step directly against a real turn
        # list, same shape ingest_intercom_conversation produces — avoids
        # needing a live Intercom API call for this test.
        turns = intercom_ingest.normalize_conversation(conversation)
        intercom_ingest._resolve_agent_identities(org_id, turns)
        ticket_id = ticket_ingest.create_ticket(org_id, source="intercom_api")
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
        ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT seq, speaker, agent_user_id FROM ticket_messages "
                    "WHERE ticket_id = %s ORDER BY seq",
                    (ticket_id,),
                ).fetchall()
        agent_row = next(r for r in rows if r["speaker"] == "agent")
        assert agent_row["agent_user_id"] == uuid.UUID(agent_kashif)

        formatted = [
            {"seq": r["seq"], "speaker": r["speaker"],
             "agent_user_id": str(r["agent_user_id"]) if r["agent_user_id"] else None, "text": ""}
            for r in rows
        ]
        spans = ticket_scoring.agent_spans(formatted)
        assert agent_kashif in {s["agent_user_id"] for s in spans}
    finally:
        admin.execute("DELETE FROM ticket_messages WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM tickets WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM ticket_agent_identity_aliases WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
