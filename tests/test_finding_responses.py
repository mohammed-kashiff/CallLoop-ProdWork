"""Finding agree/dispute: scored agent only, managers list disputes."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token
from tests.test_team_performance import AGENT_A, AGENT_B

SRC = (ROOT / "backend" / "finding_responses.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "backend" / "finding_responses_api.py").read_text(encoding="utf-8")
TICKET_ID = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, *, call_agent=None, ticket_agent=None, existing=None, ticket_exists=True):
        self.call_agent = call_agent
        self.ticket_agent = ticket_agent
        self.existing = existing
        self.ticket_exists = ticket_exists
        self.updated = None
        self.inserted = None

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if "FROM CALLS" in norm:
            if self.call_agent:
                return _Result([{"agent_user_id": self.call_agent}])
            return _Result([])
        if "FROM TICKET_AUDITS" in norm:
            if self.ticket_agent and params and self.ticket_agent == params[-1]:
                return _Result([{"agent_user_id": self.ticket_agent}])
            return _Result([])
        if "FROM TICKETS" in norm:
            return _Result([{"ok": 1}] if self.ticket_exists else [])
        if "FROM FINDING_RESPONSES" in norm and "UPDATE" not in norm and "INSERT" not in norm:
            if "STANCE = 'DISPUTE'" in norm or "STANCE = 'DISPUTE'" in norm.replace(" ", ""):
                return _Result(self.existing or [])
            if self.existing:
                return _Result(self.existing if isinstance(self.existing, list) else [self.existing])
            return _Result([])
        if "UPDATE FINDING_RESPONSES" in norm:
            self.updated = params
            row = dict(self.existing)
            row["stance"] = params[0]
            row["note"] = params[1]
            row["updated_at"] = datetime(2026, 9, 21, 13, 0, 0)
            return _Result([row])
        if "INSERT INTO FINDING_RESPONSES" in norm:
            self.inserted = params
            return _Result([{
                "id": str(uuid.uuid4()),
                "user_id": params[1],
                "channel": params[2],
                "call_id": params[3],
                "ticket_id": params[4],
                "dimension_id": params[5],
                "stance": params[6],
                "note": params[7],
                "created_at": datetime(2026, 9, 21, 12, 0, 0),
                "updated_at": datetime(2026, 9, 21, 12, 0, 0),
            }])
        return _Result([])


def _patch(monkeypatch, conn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.finding_responses.db.connection", _cm)
    return conn


def test_non_agent_cannot_respond(monkeypatch):
    from backend.finding_responses import upsert

    _patch(monkeypatch, _FakeConn(call_agent=AGENT_A))
    try:
        upsert(
            DEFAULT_ORG_ID,
            viewer_user_id=AGENT_B,
            channel="call",
            dimension_id="hold",
            stance="agree",
            call_id=42,
        )
    except PermissionError:
        return
    raise AssertionError("expected PermissionError")


def test_dispute_without_note_is_400():
    from backend.finding_responses import upsert

    try:
        upsert(
            DEFAULT_ORG_ID,
            viewer_user_id=AGENT_A,
            channel="call",
            dimension_id="hold",
            stance="dispute",
            call_id=42,
        )
    except ValueError as exc:
        assert "note" in str(exc).lower()
        return
    raise AssertionError("expected ValueError")


def test_agent_can_agree(monkeypatch):
    from backend.finding_responses import upsert

    conn = _patch(monkeypatch, _FakeConn(call_agent=AGENT_A))
    out = upsert(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_A,
        channel="call",
        dimension_id="hold",
        stance="agree",
        call_id=42,
    )
    assert out["stance"] == "agree"
    assert conn.inserted is not None


def test_http_member_cannot_list_org_disputes(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    member = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    member.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_B)}"
    r = member.get("/api/findings/responses?stance=dispute")
    assert r.status_code == 403


def test_http_manager_can_list_disputes(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    monkeypatch.setattr(
        "backend.finding_responses.list_disputes",
        lambda org_id: [{"id": "1", "stance": "dispute", "note": "too harsh", "channel": "call"}],
    )
    client = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "manager", str(user_id),
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_A)}"
    r = client.get("/api/findings/responses?stance=dispute")
    assert r.status_code == 200
    assert r.json()["responses"][0]["stance"] == "dispute"


def test_http_non_agent_put_is_403(monkeypatch):
    from backend.api import app

    monkeypatch.setattr(
        "backend.finding_responses.upsert",
        lambda *a, **k: (_ for _ in ()).throw(PermissionError("no")),
    )
    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_B, org_id=DEFAULT_ORG_ID)
    r = client.put(
        "/api/findings/response",
        json={"channel": "call", "dimension_id": "hold", "stance": "agree", "call_id": 42},
    )
    assert r.status_code == 403


def test_http_dispute_without_note_is_400(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_A, org_id=DEFAULT_ORG_ID)
    r = client.put(
        "/api/findings/response",
        json={"channel": "call", "dimension_id": "hold", "stance": "dispute", "call_id": 42},
    )
    assert r.status_code == 400


def test_revision_finding_responses_two_indexes():
    rev = ROOT / "alembic" / "versions" / "0048_finding_responses.py"
    raw = rev.read_text(encoding="utf-8")
    assert 'revision: str = "0048_finding_responses"' in raw
    assert "0047_training_assignments" in raw
    assert "uq_finding_responses_call" in raw
    assert "uq_finding_responses_ticket" in raw
    assert "COALESCE(call_id" not in raw
    assert "ENABLE ROW LEVEL SECURITY" in raw.upper()
    assert "bypass_rls" not in raw
    assert "bypass_rls" not in SRC
    assert "bypass_rls" not in API_SRC
    assert "%s" in SRC
    assert "has_note" in API_SRC
    assert '"note":' not in API_SRC
