"""IN-22/23: org owner-managed mapping from a provider's structured agent
identifier (JustCall's agent_email) to a real org_members.user_id.

Parallel to test_ticket_agent_identity_aliases.py (IN-10) — same shape,
calls instead of tickets. The backfill target here is simpler than the
ticket version: `calls` is one row per call, not per-turn, so set_alias()
updates `calls.agent_user_id` directly rather than joining through a
separate messages table."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from tests.conftest import authorize, mint_access_token

ORG_A = str(uuid.uuid4())
OLD_USER = str(uuid.uuid4())
NEW_USER = str(uuid.uuid4())


class _Result:
    def __init__(self, rows, rowcount=None):
        self._rows = rows
        self.rowcount = len(rows) if rowcount is None else rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self):
        self.aliases: dict[tuple, dict] = {}
        self.executed: list[tuple] = []
        self.calls: list[dict] = []
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
        if norm.startswith("INSERT INTO CALL_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier, uid = params
            self.aliases[(org_id, provider, identifier)] = {"user_id": uid}
            return _Result([])
        if norm.startswith("UPDATE CALLS SET AGENT_USER_ID"):
            uid, org_id, ident = params
            updated = 0
            for c in self.calls:
                if c["org_id"] == org_id and c["agent_user_id"] is None and (c["agent_identifier"] or "").lower() == ident:
                    c["agent_user_id"] = uid
                    updated += 1
            return _Result([], rowcount=updated)
        if norm.startswith("DELETE FROM CALL_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier = params
            self.aliases.pop((org_id, provider, identifier), None)
            return _Result([])
        if norm.startswith("SELECT USER_ID FROM CALL_AGENT_IDENTITY_ALIASES"):
            org_id, provider, identifier = params
            row = self.aliases.get((org_id, provider, identifier))
            return _Result([{"user_id": row["user_id"]}] if row else [])
        if norm.startswith("SELECT AGENT_IDENTIFIER, COUNT(*) AS CALL_COUNT"):
            org_id, source = params
            counts: dict[str, int] = {}
            for c in self.calls:
                if c["org_id"] != org_id or c["source"] != source or c["agent_user_id"] is not None or not c["agent_identifier"]:
                    continue
                counts[c["agent_identifier"]] = counts.get(c["agent_identifier"], 0) + 1
            rows = [{"agent_identifier": k, "call_count": v} for k, v in counts.items()]
            return _Result(sorted(rows, key=lambda r: (-r["call_count"], r["agent_identifier"])))
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

    monkeypatch.setattr("backend.call_agent_identity_aliases.db.connection", _cm)
    monkeypatch.setattr("backend.call_agent_identity_aliases.org_scope", lambda oid: _cm())
    yield conn


# ---------- set_alias / list_aliases / delete_alias ----------


def test_set_alias_creates_a_new_mapping(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        result = m.set_alias(ORG_A, "justcall", "Agent@JustCall.example", OLD_USER)
    assert result == {
        "provider": "justcall", "identifier": "agent@justcall.example",
        "user_id": OLD_USER, "backfilled_calls": 0,
    }
    assert conn.aliases[(ORG_A, "justcall", "agent@justcall.example")] == {"user_id": OLD_USER}


def test_set_alias_normalizes_case_and_whitespace(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        m.set_alias(ORG_A, "  JustCall  ", "  Agent@JustCall.example  ", OLD_USER)
    assert (ORG_A, "justcall", "agent@justcall.example") in conn.aliases


def test_set_alias_rejects_blank_identifier(monkeypatch):
    from backend import call_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.set_alias(ORG_A, "justcall", "   ", OLD_USER)
    assert exc.value.status_code == 400


def test_set_alias_rejects_invalid_user_id(monkeypatch):
    from backend import call_agent_identity_aliases as m
    from fastapi import HTTPException

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            m.set_alias(ORG_A, "justcall", "agent@justcall.example", "not-a-uuid")
    assert exc.value.status_code == 400


def test_set_alias_updates_an_existing_mapping(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "justcall", "agent@justcall.example")] = {"user_id": OLD_USER}
    with _fake_db(monkeypatch, conn):
        m.set_alias(ORG_A, "justcall", "agent@justcall.example", NEW_USER)
    assert conn.aliases[(ORG_A, "justcall", "agent@justcall.example")] == {"user_id": NEW_USER}


def test_set_alias_backfills_already_ingested_unresolved_calls(monkeypatch):
    """The IN-10/TA-15 lesson applied here from day one: a mapping must
    retroactively resolve calls already ingested under this exact
    identifier, not only future ones."""
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.calls = [
        {"org_id": ORG_A, "agent_user_id": None, "agent_identifier": "agent@justcall.example"},
        {"org_id": ORG_A, "agent_user_id": None, "agent_identifier": "agent@justcall.example"},
        # Already resolved to someone else — must not be clobbered.
        {"org_id": ORG_A, "agent_user_id": "someone-else", "agent_identifier": "agent@justcall.example"},
        # Different org — must not leak across tenants.
        {"org_id": "other-org", "agent_user_id": None, "agent_identifier": "agent@justcall.example"},
    ]
    with _fake_db(monkeypatch, conn):
        result = m.set_alias(ORG_A, "justcall", "Agent@JustCall.example", NEW_USER)
    assert result["backfilled_calls"] == 2
    assert [c["agent_user_id"] for c in conn.calls] == [NEW_USER, NEW_USER, "someone-else", None]


def test_list_aliases_returns_only_this_orgs_mappings(monkeypatch):
    from backend import call_agent_identity_aliases as m

    other_org = str(uuid.uuid4())
    conn = _FakeConn()
    conn.aliases[(ORG_A, "justcall", "a@b.com")] = {"user_id": "u1"}
    conn.aliases[(other_org, "justcall", "e@f.com")] = {"user_id": "u3"}
    with _fake_db(monkeypatch, conn):
        result = m.list_aliases(ORG_A)
    assert {r["identifier"] for r in result} == {"a@b.com"}


def test_delete_alias_removes_the_mapping(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "justcall", "a@b.com")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        m.delete_alias(ORG_A, "justcall", "a@b.com")
    assert (ORG_A, "justcall", "a@b.com") not in conn.aliases


# ---------- resolve_agent_user_id ----------


def test_resolve_agent_user_id_returns_the_mapped_id(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.aliases[(ORG_A, "justcall", "a@b.com")] = {"user_id": "u1"}
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "justcall", "A@B.com") == "u1"  # case-insensitive


def test_resolve_agent_user_id_returns_none_when_unmapped(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "justcall", "nobody@b.com") is None


def test_resolve_agent_user_id_returns_none_for_blank_identifier(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        assert m.resolve_agent_user_id(ORG_A, "justcall", None) is None
    assert conn.executed == []  # never even queries for a blank identifier


# ---------- list_unresolved_identifiers / suggest_identity_matches ----------


def test_list_unresolved_identifiers_counts_unresolved_calls(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.calls = [
        {"org_id": ORG_A, "source": "justcall", "agent_user_id": None, "agent_identifier": "a@b.com"},
        {"org_id": ORG_A, "source": "justcall", "agent_user_id": None, "agent_identifier": "a@b.com"},
        {"org_id": ORG_A, "source": "justcall", "agent_user_id": "u1", "agent_identifier": "c@d.com"},
        {"org_id": ORG_A, "source": "justcall", "agent_user_id": None, "agent_identifier": None},
    ]
    with _fake_db(monkeypatch, conn):
        result = m.list_unresolved_identifiers(ORG_A, "justcall")
    assert result == [{"identifier": "a@b.com", "turn_count": 2}]


def test_suggest_identity_matches_pairs_by_exact_email(monkeypatch):
    from backend import call_agent_identity_aliases as m

    conn = _FakeConn()
    conn.calls = [
        {"org_id": ORG_A, "source": "justcall", "agent_user_id": None, "agent_identifier": "kashif@cloop.example"},
    ]
    conn.org_directory = [
        {"org_id": ORG_A, "user_id": "u1", "email": "Kashif@Cloop.example",
         "first_name": "Kashif", "last_name": "K"},
    ]
    with _fake_db(monkeypatch, conn):
        result = m.suggest_identity_matches(ORG_A, "justcall")
    assert result[0]["suggested_user_id"] == "u1"
    assert result[0]["suggested_name"] == "Kashif K"


# ---------- justcall.agent_identity_for ----------


def test_agent_identity_for_prefers_email_over_name():
    from backend import justcall

    payload = {"data": {"agent_name": "Kashif", "agent": {"name": "Kashif", "email": "Kashif@JustCall.example"}}}
    assert justcall.agent_identity_for(payload) == "kashif@justcall.example"


def test_agent_identity_for_falls_back_to_name_when_no_email():
    from backend import justcall

    payload = {"data": {"agent_name": "Kashif", "agent": {"name": "Kashif"}}}
    assert justcall.agent_identity_for(payload) == "Kashif"


def test_agent_identity_for_none_when_nothing_present():
    from backend import justcall

    assert justcall.agent_identity_for({}) is None
    assert justcall.agent_identity_for({"data": {}}) is None


# ---------- HTTP layer: /api/calls/agent-identity-aliases ----------


def test_list_call_agent_identity_aliases_requires_auth():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/calls/agent-identity-aliases")
    assert r.status_code == 401


def test_list_call_agent_identity_aliases_is_owner_only(monkeypatch):
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
    r = client.get("/api/calls/agent-identity-aliases")
    assert r.status_code == 403


def test_set_call_agent_identity_alias_success(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.call_agent_identity_aliases_api.call_agent_identity_aliases.set_alias",
        lambda org_id, provider, identifier, uid: calls.append((provider, identifier, uid))
        or {"provider": provider, "identifier": identifier, "user_id": uid, "backfilled_calls": 0},
    )
    target = str(uuid.uuid4())
    r = client.post(
        "/api/calls/agent-identity-aliases",
        json={"provider": "justcall", "identifier": "a@b.com", "user_id": target},
    )
    assert r.status_code == 200
    assert calls[0] == ("justcall", "a@b.com", target)


def test_delete_call_agent_identity_alias_requires_owner(monkeypatch):
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
    r = client.delete("/api/calls/agent-identity-aliases/justcall/a@b.com")
    assert r.status_code == 403


def test_call_agent_identity_aliases_route_is_not_swallowed_by_call_id_route(monkeypatch):
    """Regression guard, same concern IN-10's own test guards for tickets:
    /api/calls/agent-identity-aliases must not be treated as /api/calls/{call_id}."""
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.call_agent_identity_aliases_api.call_agent_identity_aliases.list_aliases",
        lambda org_id: [],
    )
    monkeypatch.setattr(
        "backend.call_agent_identity_aliases_api.call_agent_identity_aliases.suggest_identity_matches",
        lambda org_id, provider: [],
    )
    monkeypatch.setattr(
        "backend.call_agent_identity_aliases_api.ticket_agent_aliases.list_org_agents",
        lambda org_id: [],
    )
    r = client.get("/api/calls/agent-identity-aliases")
    assert r.status_code == 200
    assert r.json() == {"aliases": [], "unresolved": [], "members": []}


# ---------- PATCH /api/calls/{call_id}/agent (IN-26) ----------


def test_reassign_call_agent_requires_owner_or_manager(monkeypatch):
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
    r = client.patch("/api/calls/1/agent", json={"agent_user_id": str(uuid.uuid4())})
    assert r.status_code == 403


def test_reassign_call_agent_404_for_missing_call(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)

    class _Conn:
        def execute(self, sql, params=None):
            return _Result([])

    @contextmanager
    def _cm(*_a, **_k):
        yield _Conn()

    monkeypatch.setattr("backend.api._conn", _cm)
    r = client.patch("/api/calls/999/agent", json={"agent_user_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_reassign_call_agent_writes_audit_log(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    new_agent = str(uuid.uuid4())
    old_agent = str(uuid.uuid4())

    class _Conn:
        def __init__(self):
            self.updated = None

        def execute(self, sql, params=None):
            norm = " ".join(str(sql).split()).upper()
            if norm.startswith("SELECT AGENT_USER_ID FROM CALLS"):
                return _Result([{"agent_user_id": old_agent}])
            if norm.startswith("UPDATE CALLS SET AGENT_USER_ID"):
                self.updated = params
                return _Result([])
            raise AssertionError(f"unexpected query: {norm}")

    conn = _Conn()

    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.api._conn", _cm)
    recorded = []
    monkeypatch.setattr(
        "backend.api.audit_log.record",
        lambda org_id, action, **kw: recorded.append((action, kw)),
    )
    r = client.patch(f"/api/calls/1/agent", json={"agent_user_id": new_agent})
    assert r.status_code == 200
    assert r.json() == {"call_id": 1, "agent_user_id": new_agent}
    assert recorded[0][0] == "call.agent_reassigned"
    assert recorded[0][1]["before"] == {"agent_user_id": old_agent}
    assert recorded[0][1]["after"] == {"agent_user_id": new_agent}
