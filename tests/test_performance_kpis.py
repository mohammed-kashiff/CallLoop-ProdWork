"""Assignable KPI targets: live-rubric catalog, org default + override, RLS."""

from __future__ import annotations

import ast
import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token
from tests.test_team_performance import AGENT_A, AGENT_B

SRC = (ROOT / "backend" / "performance_kpis.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "backend" / "performance_kpis_api.py").read_text(encoding="utf-8")

OTHER_AGENT = str(uuid.uuid4())


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
        self.executed: list[tuple[str, tuple]] = []
        self.members = [
            {"user_id": AGENT_A, "first_name": "Ada", "last_name": "Lovelace"},
            {"user_id": AGENT_B, "first_name": "Grace", "last_name": "Hopper"},
        ]
        self.rows: list[dict] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        args = tuple(params or ())
        self.executed.append((norm, args))
        if "FROM PERFORMANCE_KPIS" in norm and "SELECT CHANNEL" in norm:
            rows = list(self.rows)
            if "AGENT_USER_ID IS NULL OR AGENT_USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [
                    r for r in rows
                    if r.get("agent_user_id") is None or str(r.get("agent_user_id")) == uid
                ]
            return _Result(rows)
        if "SELECT TARGET FROM PERFORMANCE_KPIS" in norm:
            ch, dim, agent = args[1], args[2], args[3]
            for r in self.rows:
                if r["channel"] == ch and r["dimension_id"] == dim and r.get("agent_user_id") == agent:
                    return _Result([{"target": r["target"]}])
            return _Result([])
        if norm.startswith("INSERT INTO PERFORMANCE_KPIS"):
            _id, org_id, ch, dim, agent, target = args
            self.rows.append({
                "id": _id, "org_id": org_id, "channel": ch,
                "dimension_id": dim, "agent_user_id": agent, "target": target,
            })
            return _Result([])
        if norm.startswith("UPDATE PERFORMANCE_KPIS"):
            target, org_id, ch, dim, agent = args
            for r in self.rows:
                if r["channel"] == ch and r["dimension_id"] == dim and r.get("agent_user_id") == agent:
                    r["target"] = target
            return _Result([])
        if norm.startswith("DELETE FROM PERFORMANCE_KPIS"):
            _org, ch, dim, agent = args
            self.rows = [
                r for r in self.rows
                if not (r["channel"] == ch and r["dimension_id"] == dim and r.get("agent_user_id") == agent)
            ]
            return _Result([])
        if "FROM ORG_MEMBERS" in norm:
            rows = list(self.members)
            if "AND USER_ID = %S" in norm:
                uid = str(args[-1])
                rows = [r for r in rows if str(r["user_id"]) == uid]
            if "SELECT 1 FROM ORG_MEMBERS" in norm:
                uid = str(args[-1])
                rows = [r for r in self.members if str(r["user_id"]) == uid]
                return _Result([{"1": 1}] if rows else [])
            return _Result(rows)
        return _Result([])


def _patch_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.performance_kpis.db.connection", _cm)
    monkeypatch.setattr(
        "backend.performance_kpis._ticket_dimensions",
        lambda _org: [
            {"id": "problem_diagnosis", "name": "Problem Diagnosis"},
            {"id": "custom_foo", "name": "Custom Foo"},
        ],
    )
    monkeypatch.setattr(
        "backend.performance_kpis._call_dimensions",
        lambda _org: [{"id": "hold", "name": "Hold Protocol"}],
    )
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


def test_fetch_stored_is_org_scoped():
    from backend.performance_kpis import fetch_stored

    conn = _FakeConn()
    fetch_stored(conn, DEFAULT_ORG_ID, only_agent_id=AGENT_A)
    sql, args = conn.executed[0]
    assert "ORG_ID = %S" in sql
    assert args[0] == DEFAULT_ORG_ID
    assert args[-1] == AGENT_A
    assert "BYPASS_RLS" not in sql


def test_sql_is_parameterized_and_never_selects_email():
    assert "f\"SELECT" not in SRC
    assert "f'SELECT" not in SRC
    assert "org_id = %s" in SRC
    for line in SRC.splitlines():
        stripped = line.strip().upper()
        if "SELECT" in stripped:
            assert "EMAIL" not in stripped


def test_catalog_merges_custom_dim_with_null_target(monkeypatch):
    from backend.performance_kpis import catalog

    conn = _patch_db(monkeypatch, _FakeConn())
    conn.rows = [
        {"channel": "ticket", "dimension_id": "problem_diagnosis", "agent_user_id": None, "target": 80},
    ]
    body = catalog(DEFAULT_ORG_ID, viewer_user_id=AGENT_A, is_manager=True)
    ids = [d["id"] for d in body["tickets"]["dimensions"]]
    assert ids[0] == "__overall__"
    assert "custom_foo" in ids
    custom = next(d for d in body["tickets"]["dimensions"] if d["id"] == "custom_foo")
    assert custom["org_target"] is None
    diag = next(d for d in body["tickets"]["dimensions"] if d["id"] == "problem_diagnosis")
    assert diag["org_target"] == 80
    assert {m["user_id"] for m in body["roster"]} == {AGENT_A, AGENT_B}
    assert "email" not in body["roster"][0]


def test_override_beats_org_default(monkeypatch):
    from backend.performance_kpis import effective_target

    rows = [
        {"channel": "ticket", "dimension_id": "diag", "agent_user_id": None, "target": 80},
        {"channel": "ticket", "dimension_id": "diag", "agent_user_id": AGENT_A, "target": 50},
    ]
    assert effective_target(rows, "ticket", "diag", AGENT_A) == 50
    assert effective_target(rows, "ticket", "diag", AGENT_B) == 80
    assert effective_target(rows, "ticket", "diag", None) == 80


def test_member_catalog_hides_teammate_overrides(monkeypatch):
    from backend.performance_kpis import catalog

    conn = _patch_db(monkeypatch, _FakeConn())
    conn.rows = [
        {"channel": "ticket", "dimension_id": "custom_foo", "agent_user_id": None, "target": 70},
        {"channel": "ticket", "dimension_id": "custom_foo", "agent_user_id": AGENT_A, "target": 90},
        {"channel": "ticket", "dimension_id": "custom_foo", "agent_user_id": AGENT_B, "target": 40},
    ]
    body = catalog(DEFAULT_ORG_ID, viewer_user_id=AGENT_B, is_manager=False)
    assert body["roster"] == []
    foo = next(d for d in body["tickets"]["dimensions"] if d["id"] == "custom_foo")
    assert foo["org_target"] == 70
    assert [o["user_id"] for o in foo["overrides"]] == [AGENT_B]


def test_upsert_sets_and_clears(monkeypatch):
    from backend.performance_kpis import catalog, upsert

    conn = _patch_db(monkeypatch, _FakeConn())
    monkeypatch.setattr("backend.performance_kpis.audit_log.record", lambda *a, **k: None)
    out = upsert(
        DEFAULT_ORG_ID,
        channel="ticket",
        dimension_id="custom_foo",
        agent_user_id=None,
        target=82,
    )
    assert out["target"] == 82
    body = catalog(DEFAULT_ORG_ID, viewer_user_id=AGENT_A, is_manager=True)
    foo = next(d for d in body["tickets"]["dimensions"] if d["id"] == "custom_foo")
    assert foo["org_target"] == 82
    cleared = upsert(
        DEFAULT_ORG_ID,
        channel="ticket",
        dimension_id="custom_foo",
        agent_user_id=None,
        target=None,
    )
    assert cleared["target"] is None
    body2 = catalog(DEFAULT_ORG_ID, viewer_user_id=AGENT_A, is_manager=True)
    foo2 = next(d for d in body2["tickets"]["dimensions"] if d["id"] == "custom_foo")
    assert foo2["org_target"] is None


def test_invalid_dimension_is_400(monkeypatch):
    from backend.performance_kpis import upsert
    from fastapi import HTTPException

    _patch_db(monkeypatch, _FakeConn())
    try:
        upsert(
            DEFAULT_ORG_ID,
            channel="ticket",
            dimension_id="not_a_real_dim",
            agent_user_id=None,
            target=80,
        )
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected 400")


def test_unknown_agent_is_400(monkeypatch):
    from backend.performance_kpis import upsert
    from fastapi import HTTPException

    _patch_db(monkeypatch, _FakeConn())
    try:
        upsert(
            DEFAULT_ORG_ID,
            channel="ticket",
            dimension_id="custom_foo",
            agent_user_id=OTHER_AGENT,
            target=80,
        )
    except HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("expected 400")


def test_http_member_cannot_put(monkeypatch):
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
    r = member.put(
        "/api/performance-kpis",
        json={"channel": "ticket", "dimension_id": "custom_foo", "target": 80},
    )
    assert r.status_code == 403


def test_http_owner_get_and_put(monkeypatch):
    from backend.api import app

    captured = {}

    def _cat(org_id, *, viewer_user_id, is_manager):
        captured["get_manager"] = is_manager
        captured["org_id"] = org_id
        return {
            "tickets": {"dimensions": [{"id": "__overall__", "name": "Overall score", "org_target": None, "overrides": []}]},
            "calls": {"dimensions": []},
            "roster": [{"user_id": viewer_user_id, "display_name": "Ada Lovelace"}],
        }

    def _up(org_id, *, channel, dimension_id, agent_user_id, target):
        captured["put"] = {
            "org_id": org_id, "channel": channel, "dim": dimension_id,
            "agent": agent_user_id, "target": target,
        }
        return {
            "channel": channel, "dimension_id": dimension_id,
            "agent_user_id": agent_user_id, "target": target,
        }

    monkeypatch.setattr("backend.performance_kpis.catalog", _cat)
    monkeypatch.setattr("backend.performance_kpis.upsert", _up)

    owner = TestClient(app)
    authorize(owner, monkeypatch, sub=AGENT_A, org_id=DEFAULT_ORG_ID)
    r = owner.get("/api/performance-kpis")
    assert r.status_code == 200
    assert captured["get_manager"] is True
    assert captured["org_id"] == DEFAULT_ORG_ID
    assert "email" not in r.json()["roster"][0]

    r2 = owner.put(
        "/api/performance-kpis",
        json={"channel": "call", "dimension_id": "hold", "target": 85, "agent_user_id": AGENT_B},
    )
    assert r2.status_code == 200
    assert captured["put"]["channel"] == "call"
    assert captured["put"]["dim"] == "hold"
    assert captured["put"]["agent"] == AGENT_B
    assert captured["put"]["target"] == 85


def test_http_requires_auth():
    from backend.api import app

    r = TestClient(app).get("/api/performance-kpis")
    assert r.status_code in (401, 403)
