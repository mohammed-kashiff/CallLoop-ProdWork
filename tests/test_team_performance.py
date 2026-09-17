"""Team Performance Dashboard: org-scoped rollups, role view-scope, no RLS bypass."""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager
from datetime import date

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token

SRC = (ROOT / "backend" / "team_performance.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "backend" / "team_performance_api.py").read_text(encoding="utf-8")

AGENT_A = str(uuid.uuid4())
AGENT_B = str(uuid.uuid4())
AGENT_GONE = str(uuid.uuid4())


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
        self.ticket_avgs = [
            {"agent_user_id": AGENT_A, "avg_score": 90.0, "n": 2},
            {"agent_user_id": AGENT_B, "avg_score": 70.0, "n": 1},
            {"agent_user_id": AGENT_GONE, "avg_score": 50.0, "n": 1},
        ]
        self.call_avgs = [
            {"agent_user_id": AGENT_A, "avg_score": 80.0, "n": 1},
            {"agent_user_id": None, "avg_score": 40.0, "n": 2},
        ]
        self.ticket_weeks = [
            {"week": date(2026, 9, 14), "avg_score": 85.0, "n": 3},
        ]
        self.call_weeks = [
            {"week": date(2026, 9, 14), "avg_score": 60.0, "n": 3},
        ]
        self.ticket_total = 10
        self.call_total = 6
        self.ticket_heatmap = [
            {"agent_user_id": AGENT_A, "dim_id": "tone", "dim_name": "Tone & Empathy", "pass_n": 2, "n": 2},
            {"agent_user_id": AGENT_A, "dim_id": "diag", "dim_name": "Problem Diagnosis", "pass_n": 0, "n": 1},
            {"agent_user_id": AGENT_A, "dim_id": "clarity", "dim_name": "Communication Clarity", "pass_n": 0, "n": 1},
            {"agent_user_id": AGENT_B, "dim_id": "own", "dim_name": "Ownership & Handoff Quality", "pass_n": 1, "n": 1},
        ]
        self.call_heatmap = [
            {"agent_user_id": AGENT_A, "dim_id": "greeting", "dim_name": "Greeting", "pass_n": 1, "n": 1},
            {"agent_user_id": AGENT_A, "dim_id": "hold", "dim_name": "Hold Protocol", "pass_n": 0, "n": 1},
        ]
        self.call_findings = [
            {
                "agent_user_id": AGENT_A,
                "findings": {
                    "findings": [
                        {"id": "greeting", "name": "Greeting", "verdict": "pass", "weight": 10},
                        {"id": "hold", "name": "Hold Protocol", "verdict": "fail", "weight": 15},
                    ]
                },
            },
        ]
        self.ticket_findings = [
            {
                "agent_user_id": AGENT_A,
                "findings": {
                    "findings": [
                        {"id": "tone", "name": "Tone & Empathy", "verdict": "pass", "weight": 17},
                        {"id": "diag", "name": "Problem Diagnosis", "verdict": "fail", "weight": 22},
                    ]
                },
            },
            {
                "agent_user_id": AGENT_A,
                "findings": {
                    "findings": [
                        {"id": "tone", "name": "Tone & Empathy", "verdict": "pass", "weight": 17},
                        {"id": "clarity", "name": "Communication Clarity", "verdict": "fail", "weight": 10},
                    ]
                },
            },
            {
                "agent_user_id": AGENT_B,
                "findings": {
                    "findings": [
                        {"id": "own", "name": "Ownership & Handoff Quality", "verdict": "pass", "weight": 17},
                    ]
                },
            },
        ]

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        args = tuple(params or ())
        self.executed.append((norm, args))
        if "JSONB_ARRAY_ELEMENTS" in norm:
            if "FROM TICKET_AUDITS" in norm:
                rows = list(self.ticket_heatmap)
            elif "INNER JOIN LATEST" in norm:
                rows = list(self.call_heatmap)
            else:
                rows = []
            if "AND TA.AGENT_USER_ID = %S" in norm or "AND C.AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if r.get("agent_user_id") and str(r["agent_user_id"]) == uid]
            return _Result(rows)
        if "FROM ORG_MEMBERS" in norm:
            rows = list(self.members)
            if "AND USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if str(r["user_id"]) == uid]
            return _Result(rows)
        if "FROM TICKET_AUDITS" in norm:
            if "FINDINGS" in norm:
                rows = list(self.ticket_findings)
            elif "DATE_TRUNC" in norm:
                rows = list(self.ticket_weeks)
            else:
                rows = list(self.ticket_avgs)
            if "AND AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if str(r.get("agent_user_id")) == uid]
            return _Result(rows)
        if "INNER JOIN LATEST" in norm:
            if "FINDINGS" in norm:
                rows = list(self.call_findings)
            elif "DATE_TRUNC" in norm:
                rows = list(self.call_weeks)
            else:
                rows = list(self.call_avgs)
            if "AND C.AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if r.get("agent_user_id") and str(r["agent_user_id"]) == uid]
            return _Result(rows)
        if "FROM TICKETS" in norm:
            return _Result([{"n": self.ticket_total}])
        if "FROM CALLS" in norm:
            return _Result([{"n": self.call_total}])
        return _Result([])


def _patch_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.team_performance.db.connection", _cm)
    return conn


def test_clamp_days():
    from backend.team_performance import clamp_days

    assert clamp_days(None) == 30
    assert clamp_days("nope") == 30
    assert clamp_days(0) == 1
    assert clamp_days(9999) == 365
    assert clamp_days(7) == 7


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
    assert "INTERVAL '1 day'" in SRC
    for line in SRC.splitlines():
        stripped = line.strip().upper()
        if "SELECT" in stripped and ("FROM ORG_MEMBERS" in stripped or stripped.startswith("SELECT")):
            assert "EMAIL" not in stripped


def test_manager_snapshot_includes_team_unassigned_and_ticket_highlights(monkeypatch):
    from backend.team_performance import snapshot

    conn = _patch_db(monkeypatch, _FakeConn())
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_A,
        is_manager=True,
        days=30,
    )
    assert body["view_scope"] == "team"
    assert body["days"] == 30
    names = {a["display_name"] for a in body["agents"]}
    assert names == {"Ada Lovelace", "Grace Hopper", "Unassigned calls", "Former teammate"}
    ada = next(a for a in body["agents"] if a["user_id"] == AGENT_A)
    assert ada["tickets"]["avg_score"] == 90.0
    assert ada["top_strength"]["id"] == "tone"
    assert ada["top_gap"]["id"] == "diag"
    assert ada["call_top_strength"]["id"] == "greeting"
    assert ada["call_top_gap"]["id"] == "hold"
    ticket_hm = body["heatmap"]["tickets"]
    assert {d["id"] for d in ticket_hm["dimensions"]} >= {
        "problem_diagnosis", "tone", "diag", "clarity", "own",
    }
    assert all(row["user_id"] is not None for row in ticket_hm["rows"])
    ada_ticket = next(r for r in ticket_hm["rows"] if r["user_id"] == AGENT_A)
    tone = next(c for c in ada_ticket["cells"] if c["id"] == "tone")
    diag = next(c for c in ada_ticket["cells"] if c["id"] == "diag")
    assert tone["pass"] == 2 and tone["n"] == 2 and tone["rate"] == 1.0
    assert diag["pass"] == 0 and diag["n"] == 1 and diag["rate"] == 0.0
    call_hm = body["heatmap"]["calls"]
    ada_call = next(r for r in call_hm["rows"] if r["user_id"] == AGENT_A)
    hold = next(c for c in ada_call["cells"] if c["id"] == "hold")
    assert hold["pass"] == 0 and hold["n"] == 1 and hold["rate"] == 0.0
    assert body["org"]["tickets"]["total"] == 10
    assert body["org"]["calls"]["total"] == 6
    unassigned = next(a for a in body["agents"] if a["user_id"] is None)
    assert unassigned["calls"]["count"] == 2
    assert unassigned["top_strength"] is None
    assert body["weekly"][0]["ticket_avg"] == 85.0
    assert body["weekly"][0]["call_avg"] == 60.0
    for sql, params in conn.executed:
        assert "%S" in sql
        assert DEFAULT_ORG_ID in params
        joined = " ".join(str(p) for p in params)
        assert "@" not in joined


def test_member_snapshot_scopes_sql_and_hides_teammates(monkeypatch):
    from backend.team_performance import snapshot

    conn = _patch_db(monkeypatch, _FakeConn())
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_B,
        is_manager=False,
        days=30,
    )
    assert body["view_scope"] == "own"
    assert [a["user_id"] for a in body["agents"]] == [AGENT_B]
    assert all(a["display_name"] != "Unassigned calls" for a in body["agents"])
    assert [r["user_id"] for r in body["heatmap"]["tickets"]["rows"]] == [AGENT_B]
    assert [r["user_id"] for r in body["heatmap"]["calls"]["rows"]] == [AGENT_B]
    scoped = [params for sql, params in conn.executed if "AND AGENT_USER_ID = %S" in sql or "AND USER_ID = %S" in sql]
    assert scoped
    assert all(params[-1] == AGENT_B for params in scoped)


def test_http_owner_sees_team_member_sees_own(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    captured = {}

    def _snap(org_id, *, viewer_user_id, is_manager, days):
        captured["is_manager"] = is_manager
        captured["viewer"] = viewer_user_id
        captured["days"] = days
        captured["org_id"] = org_id
        return {
            "view_scope": "team" if is_manager else "own",
            "days": days,
            "org": {"tickets": {"avg_score": 80.0, "count": 1, "total": 2}, "calls": {"avg_score": None, "count": 0, "total": 0}},
            "weekly": [],
            "agents": [{
                "user_id": viewer_user_id,
                "display_name": "Ada Lovelace",
                "role": "owner" if is_manager else "member",
                "tickets": {"avg_score": 80.0, "count": 1},
                "calls": {"avg_score": None, "count": 0},
                "top_strength": None,
                "top_gap": None,
                "call_top_strength": None,
                "call_top_gap": None,
            }],
            "heatmap": {
                "tickets": {"dimensions": [], "rows": []},
                "calls": {"dimensions": [], "rows": []},
            },
        }

    monkeypatch.setattr("backend.team_performance.snapshot", _snap)

    owner = TestClient(app)
    authorize(owner, monkeypatch, sub=AGENT_A, org_id=DEFAULT_ORG_ID)
    r = owner.get("/api/team-performance?days=90")
    assert r.status_code == 200
    assert r.json()["view_scope"] == "team"
    assert captured["is_manager"] is True
    assert captured["days"] == 90
    assert captured["org_id"] == DEFAULT_ORG_ID
    assert "email" not in r.json()["agents"][0]

    member = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    member.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_B)}"
    r2 = member.get("/api/team-performance?days=9999")
    assert r2.status_code == 200
    assert r2.json()["view_scope"] == "own"
    assert captured["is_manager"] is False
    assert captured["days"] == 365
    assert captured["viewer"] == AGENT_B


def test_heatmap_empty_window_has_canonical_dims_and_null_rates(monkeypatch):
    from backend.team_performance import snapshot

    conn = _FakeConn()
    conn.ticket_heatmap = []
    conn.call_heatmap = []
    conn.ticket_findings = []
    conn.call_findings = []
    conn.ticket_avgs = []
    conn.call_avgs = []
    _patch_db(monkeypatch, conn)
    body = snapshot(
        DEFAULT_ORG_ID,
        viewer_user_id=AGENT_A,
        is_manager=True,
        days=7,
    )
    ticket_ids = [d["id"] for d in body["heatmap"]["tickets"]["dimensions"]]
    assert ticket_ids[:5] == [
        "problem_diagnosis",
        "resolution_correctness",
        "communication_clarity",
        "tone_and_empathy",
        "ownership_and_handoff_quality",
    ]
    assert "response_timeliness" not in ticket_ids
    for row in body["heatmap"]["tickets"]["rows"]:
        assert row["user_id"] is not None
        assert all(c["n"] == 0 and c["rate"] is None for c in row["cells"])


def test_http_requires_auth():
    from backend.api import app

    r = TestClient(app).get("/api/team-performance")
    assert r.status_code in (401, 403)
