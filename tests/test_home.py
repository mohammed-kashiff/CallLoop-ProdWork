"""GET /api/home is always the viewer's own week."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize, mint_access_token
from tests.test_team_performance import AGENT_A, AGENT_B

SRC = (ROOT / "backend" / "home.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "backend" / "home_api.py").read_text(encoding="utf-8")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def execute(self, sql, params=None):
        return _Result([])


def _empty_home(viewer: str) -> dict:
    return {
        "user_id": viewer,
        "days": 30,
        "scores": {
            "tickets": {"avg_score": None, "count": 0, "target": None},
            "calls": {"avg_score": None, "count": 0, "target": None},
        },
        "flags": [],
        "drills": [],
        "tickets": [],
    }


def test_home_forces_member_scope_even_for_owners(monkeypatch):
    from backend.home import snapshot

    captured = {}

    def _perf(org_id, *, viewer_user_id, is_manager, days):
        captured["perf"] = {
            "org_id": org_id, "viewer": viewer_user_id,
            "is_manager": is_manager, "days": days,
        }
        return {
            "agents": [{
                "user_id": viewer_user_id,
                "tickets": {"avg_score": 88.0, "count": 2},
                "calls": {"avg_score": 70.0, "count": 1},
            }],
            "org": {},
        }

    def _train(org_id, *, viewer_user_id, is_manager, days, **_k):
        captured["train"] = {"viewer": viewer_user_id, "is_manager": is_manager}
        return {"drills": []}

    monkeypatch.setattr("backend.home.team_performance.snapshot", _perf)
    monkeypatch.setattr("backend.home.training.snapshot", _train)
    monkeypatch.setattr("backend.home.training.list_open_assignments", lambda *a, **k: [])
    monkeypatch.setattr(
        "backend.home.performance_kpis.fetch_stored", lambda *a, **k: [],
    )
    monkeypatch.setattr(
        "backend.home.performance_kpis.effective_target", lambda *a, **k: 80.0,
    )

    @contextmanager
    def _cm(*_a, **_k):
        yield _FakeConn()

    monkeypatch.setattr("backend.home.db.connection", _cm)
    body = snapshot(DEFAULT_ORG_ID, AGENT_A)
    assert captured["perf"]["is_manager"] is False
    assert captured["train"]["is_manager"] is False
    assert captured["perf"]["viewer"] == AGENT_A
    assert body["user_id"] == AGENT_A
    assert body["scores"]["tickets"]["avg_score"] == 88.0
    assert all(row.get("user_id") == AGENT_A for row in [{"user_id": body["user_id"]}])


def test_http_home_is_self_scoped_for_manager(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    captured = {}

    def _snap(org_id, viewer_user_id):
        captured["org_id"] = org_id
        captured["viewer"] = viewer_user_id
        return _empty_home(viewer_user_id)

    monkeypatch.setattr("backend.home.snapshot", _snap)
    client = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "manager", str(user_id),
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=AGENT_B)}"
    r = client.get("/api/home")
    assert r.status_code == 200
    assert r.json()["user_id"] == AGENT_B
    assert captured["viewer"] == AGENT_B
    assert captured["org_id"] == DEFAULT_ORG_ID


def test_http_home_member_payload_is_own_user(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    monkeypatch.setattr("backend.home.snapshot", lambda org, viewer: _empty_home(viewer))
    client = TestClient(app)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            DEFAULT_ORG_ID, "member", str(user_id),
        ),
    )
    uid = str(uuid.uuid4())
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.get("/api/home")
    assert r.status_code == 200
    assert r.json()["user_id"] == uid


def test_admin_host_still_skips_customer_home():
    app = (ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    assert "adminHost ? <Admin /> : <Home />" in app


def test_home_module_no_rls_bypass_and_self_scope():
    assert "bypass_rls" not in SRC
    assert "bypass_rls" not in API_SRC
    assert "is_manager=False" in SRC
    assert "%s" in SRC
    assert "org_id_from_request" in API_SRC
    assert "user_id_from_request" in API_SRC
