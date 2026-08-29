"""Profile: self-scoped name PATCH and usage GET. JWT org/user only."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

ORG_B = "00000000-0000-4000-8000-0000000000bb"


class _UpdateCursor:
    def __init__(self, rowcount=1):
        self.rowcount = rowcount


class _UpdateConn:
    def __init__(self):
        self.calls: list[tuple] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return _UpdateCursor(1)


def test_me_usage_uses_jwt_org_not_query(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    seen: list[str] = []

    def _usage(org_id):
        seen.append(org_id or "")
        return {
            "org_id": org_id,
            "usage": {},
            "cost": {"pyai_usd": 0, "claude_usd": 0, "total_usd": 0},
            "features": {},
        }

    monkeypatch.setattr("backend.admin_console.usage_for_org", _usage)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/me/usage", params={"org_id": ORG_B})
    assert r.status_code == 200
    assert seen == [DEFAULT_ORG_ID]
    assert r.json()["org_id"] == DEFAULT_ORG_ID
    assert r.json()["org_id"] != ORG_B


def test_me_usage_does_not_require_platform_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    monkeypatch.setattr(
        "backend.admin_console.usage_for_org",
        lambda org_id: {
            "org_id": org_id,
            "usage": {},
            "cost": {"pyai_usd": 1, "claude_usd": 0, "total_usd": 1},
            "features": {},
        },
    )
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/me/usage")
    assert r.status_code == 200
    assert r.json()["cost"]["total_usd"] == 1


def test_patch_me_updates_caller_membership_only(monkeypatch):
    conn = _UpdateConn()

    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.db.connection", _cm)
    monkeypatch.setattr("backend.db.apply_tenant_gucs", lambda *a, **k: None)
    from backend.api import app

    client = TestClient(app)
    uid = authorize(client, monkeypatch)
    forged = str(uuid.uuid4())
    r = client.patch(
        "/api/me",
        json={
            "first_name": "  Arya  ",
            "last_name": "Stark",
            "user_id": forged,
        },
    )
    assert r.status_code == 200
    assert r.json() == {"first_name": "Arya", "last_name": "Stark"}
    assert len(conn.calls) == 1
    _sql, params = conn.calls[0]
    assert "UPDATE org_members" in " ".join(str(_sql).split())
    assert params == ("Arya", "Stark", uid, DEFAULT_ORG_ID)
    assert forged not in params


def test_patch_me_rejects_blank_names(monkeypatch):
    monkeypatch.setattr("backend.admin_console.usage_for_org", MagicMock())
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.patch("/api/me", json={"first_name": "  ", "last_name": "Stark"})
    assert r.status_code == 400
