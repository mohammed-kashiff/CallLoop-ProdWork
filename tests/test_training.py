"""Training drills: JWT org scope, member vs manager agent=, no RLS bypass."""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager
from datetime import datetime

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token
from tests.test_team_performance import AGENT_A, AGENT_B

SRC = (ROOT / "backend" / "training.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "backend" / "training_api.py").read_text(encoding="utf-8")

TICKET_ID = str(uuid.uuid4())
OTHER_ORG_AGENT = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self):
        self.executed: list[tuple[str, tuple]] = []
        self.members = [
            {"user_id": AGENT_A, "role": "owner", "first_name": "Ada", "last_name": "Lovelace"},
            {"user_id": AGENT_B, "role": "member", "first_name": "Grace", "last_name": "Hopper"},
        ]
        self.ticket_findings = [
            {
                "agent_user_id": AGENT_A,
                "findings": {
                    "findings": [
                        {"id": "diag", "name": "Problem Diagnosis", "verdict": "fail", "weight": 22},
                        {"id": "tone", "name": "Tone & Empathy", "verdict": "pass", "weight": 17},
                    ]
                },
            },
            {
                "agent_user_id": AGENT_B,
                "findings": {
                    "findings": [
                        {"id": "own", "name": "Ownership & Handoff Quality", "verdict": "fail", "weight": 17},
                    ]
                },
            },
        ]
        self.call_findings = [
            {
                "agent_user_id": AGENT_A,
                "findings": {
                    "findings": [
                        {"id": "hold", "name": "Hold Protocol", "verdict": "fail", "weight": 15},
                    ]
                },
            },
        ]
        self.ticket_drills = [
            {
                "ticket_id": TICKET_ID,
                "created_at": datetime(2026, 9, 17, 12, 0, 0),
                "dim_id": "diag",
                "dim_name": "Problem Diagnosis",
                "reasoning": "Missed the root cause.",
                "evidence_text": "Customer repeated the outage twice.",
                "coaching_note": None,
            },
        ]
        self.call_drills = [
            {
                "call_id": 42,
                "created_at": datetime(2026, 9, 16, 9, 0, 0),
                "dim_id": "hold",
                "dim_name": "Hold Protocol",
                "reasoning": "Left the caller without a check-back.",
                "evidence_text": None,
                "coaching_note": "Announce hold time and come back.",
            },
        ]

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        args = tuple(params or ())
        self.executed.append((norm, args))
        if "JSONB_ARRAY_ELEMENTS" in norm:
            if "FROM TICKET_AUDITS" in norm:
                rows = list(self.ticket_drills)
                agent = str(args[1]) if len(args) > 1 else ""
                dim = str(args[3]) if len(args) > 3 else ""
                if agent != AGENT_A:
                    rows = []
                elif dim:
                    rows = [r for r in rows if str(r.get("dim_id")) == dim]
            elif "INNER JOIN LATEST" in norm:
                rows = list(self.call_drills)
                agent = str(args[2]) if len(args) > 2 else ""
                dim = str(args[4]) if len(args) > 4 else ""
                if agent != AGENT_A:
                    rows = []
                elif dim:
                    rows = [r for r in rows if str(r.get("dim_id")) == dim]
            else:
                rows = []
            return _Result(rows)
        if "FROM ORG_MEMBERS" in norm:
            rows = list(self.members)
            if "AND USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if str(r["user_id"]) == uid]
            return _Result(rows)
        if "FROM TICKET_AUDITS" in norm and "FINDINGS" in norm:
            rows = list(self.ticket_findings)
            if "AND AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if str(r.get("agent_user_id")) == uid]
            return _Result(rows)
        if "INNER JOIN LATEST" in norm:
            rows = list(self.call_findings)
            if "AND C.AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if r.get("agent_user_id") and str(r["agent_user_id"]) == uid]
            return _Result(rows)
        return _Result([])


def _patch_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.team_performance.db.connection", _cm)
    monkeypatch.setattr("backend.training.db.connection", _cm)
    return conn


def test_module_does_not_bypass_rls_or_import_qa_engine():
    assert "bypass_rls" not in SRC
    assert "bypass_rls" not in API_SRC
    tree = ast.parse(SRC)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "qa_engine" not in imported
    assert "qa_v8" not in imported


def test_sql_is_parameterized_and_never_selects_email():
    assert "f\"SELECT" not in SRC
    assert "f'SELECT" not in SRC
    assert "org_id = %s" in SRC
    for line in SRC.splitlines():
        stripped = line.strip().upper()
        if "SELECT" in stripped:
            assert "EMAIL" not in stripped


def test_manager_default_uses_own_top_gap_drills(monkeypatch):
    from backend.training import snapshot

    conn = _patch_db(monkeypatch, _FakeConn())
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_A,
        is_manager=True,
        days=30,
    )
    assert body["view_scope"] == "team"
    assert body["agent"]["user_id"] == AGENT_A
    assert body["agent"]["display_name"] == "Ada Lovelace"
    assert {m["user_id"] for m in body["roster"]} == {AGENT_A, AGENT_B}
    assert body["focus"]["ticket"]["id"] == "diag"
    assert body["focus"]["call"]["id"] == "hold"
    assert [d["channel"] for d in body["drills"]] == ["ticket", "call"]
    assert body["drills"][0]["ticket_id"] == TICKET_ID
    assert body["drills"][0]["reasoning"] == "Missed the root cause."
    assert body["drills"][1]["call_id"] == 42
    assert body["drills"][1]["coaching_note"] == "Announce hold time and come back."
    assert "email" not in body["agent"]
    for sql, params in conn.executed:
        assert "%S" in sql
        joined = " ".join(str(p) for p in params)
        assert "@" not in joined


def test_member_cannot_load_teammate_drills(monkeypatch):
    from backend.training import snapshot

    _patch_db(monkeypatch, _FakeConn())
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_B,
        is_manager=False,
        days=30,
        agent=AGENT_A,
        dim="diag",
        channel="ticket",
    )
    assert body["view_scope"] == "own"
    assert body["agent"]["user_id"] == AGENT_B
    assert body["agent"]["display_name"] == "Grace Hopper"
    assert body["roster"] == []
    assert body["drills"] == []


def test_manager_unknown_agent_raises(monkeypatch):
    from backend.training import snapshot

    _patch_db(monkeypatch, _FakeConn())
    try:
        snapshot(
            DEFAULT_ORG_ID,
            viewer_user_id=AGENT_A,
            is_manager=True,
            days=30,
            agent=OTHER_ORG_AGENT,
        )
    except ValueError as exc:
        assert "Unknown teammate" in str(exc)
    else:
        raise AssertionError("expected unknown teammate")


def test_heatmap_override_scopes_channel(monkeypatch):
    from backend.training import snapshot

    _patch_db(monkeypatch, _FakeConn())
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_A,
        is_manager=True,
        days=30,
        channel="ticket",
        dim="diag",
        agent=AGENT_A,
    )
    assert body["focus"]["ticket"]["id"] == "diag"
    assert body["focus"]["call"] is None
    assert all(d["channel"] == "ticket" for d in body["drills"])


def test_http_member_ignores_agent_query(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    captured = {}

    def _snap(org_id, *, viewer_user_id, is_manager, days, channel=None, dim=None, agent=None):
        captured["is_manager"] = is_manager
        captured["viewer"] = viewer_user_id
        captured["agent"] = agent
        captured["org_id"] = org_id
        return {
            "view_scope": "own",
            "days": days,
            "agent": {"user_id": viewer_user_id, "display_name": "Grace Hopper", "role": "member"},
            "roster": [],
            "focus": {"ticket": None, "call": None, "channel": channel, "dim": dim},
            "drills": [],
        }

    monkeypatch.setattr("backend.training.snapshot", _snap)

    member = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    member.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_B)}"
    r = member.get(f"/api/training?days=30&agent={AGENT_A}&dim=diag&channel=ticket")
    assert r.status_code == 200
    assert captured["is_manager"] is False
    assert captured["viewer"] == AGENT_B
    assert captured["agent"] is None
    assert captured["org_id"] == DEFAULT_ORG_ID
    assert "email" not in r.json()["agent"]


def test_http_bad_channel_is_400(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, sub=AGENT_A, org_id=DEFAULT_ORG_ID)
    r = client.get("/api/training?channel=sms")
    assert r.status_code == 400


def test_http_requires_auth():
    from backend.api import app

    r = TestClient(app).get("/api/training")
    assert r.status_code in (401, 403)
