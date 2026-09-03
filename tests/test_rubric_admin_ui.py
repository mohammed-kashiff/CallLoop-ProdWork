"""CR-14: admin console Rubric tab — read endpoint (GET /api/admin/orgs/{org_id}/rubric).

Save is CR-13's write route on the same resource path (POST, body
{"weights": {...}}, same response shape as this GET) — built separately.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, row=None):
        self.row = row
        self.queries: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.queries.append((norm, params))
        assert "FROM RUBRICS WHERE ORG_ID" in norm
        assert "IS_ACTIVE" in norm
        return _Result([self.row] if self.row else [])


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.admin_console.db.connection", _connection)
    yield conn


def test_rubric_for_org_returns_legacy_source_when_no_active_row(monkeypatch):
    from backend.admin_console import rubric_for_org
    from backend.audit_store import load_v8_definition
    from backend.qa_v8 import list_dimensions

    conn = _FakeConn(row=None)
    with _fake_db(monkeypatch, conn):
        out = rubric_for_org(ORG_A)
    assert out["source"] == "legacy"
    assert out["rubric_id"] is None
    assert out["version"] is None
    assert out["updated_at"] is None
    expected = {d["id"]: d["weight"] for d in list_dimensions(load_v8_definition())}
    assert out["weights"] == expected


def test_rubric_for_org_returns_custom_source_and_weights_when_active_row_exists(monkeypatch):
    from backend.admin_console import rubric_for_org
    from backend.audit_store import load_v8_definition

    custom = load_v8_definition()
    custom["technical_skills"]["dimensions"][0]["weight"] = 10
    custom["soft_skills"]["dimensions"][0]["weight"] = 50
    rid = str(uuid.uuid4())
    conn = _FakeConn(
        row={
            "id": rid, "version": 3, "definition": custom,
            "updated_at": datetime(2026, 2, 1, tzinfo=timezone.utc),
        },
    )
    with _fake_db(monkeypatch, conn):
        out = rubric_for_org(ORG_A)
    assert out["source"] == "custom"
    assert out["rubric_id"] == rid
    assert out["version"] == 3
    assert out["weights"]["resolution_effectiveness"] == 10
    assert out["weights"]["active_listening"] == 50


def test_rubric_for_org_rejects_bad_org_id():
    from fastapi import HTTPException
    import pytest

    from backend.admin_console import rubric_for_org

    with pytest.raises(HTTPException) as exc:
        rubric_for_org("not-a-uuid")
    assert exc.value.status_code == 400


def test_rubric_route_is_gated_and_forwards_org(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[str] = []

    def _rubric_for_org(org_id):
        seen.append(org_id)
        return {
            "org_id": org_id, "source": "legacy", "rubric_id": None,
            "version": None, "updated_at": None, "weights": {},
        }

    monkeypatch.setattr("backend.api.admin_console.rubric_for_org", _rubric_for_org)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get(f"/api/admin/orgs/{ORG_A}/rubric")
    assert r.status_code == 200
    assert seen == [ORG_A]
    assert r.json()["org_id"] == ORG_A


def test_rubric_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get(f"/api/admin/orgs/{ORG_A}/rubric").status_code == 403
    assert client.post(
        f"/api/admin/orgs/{ORG_A}/rubric",
        json={"weights": {
            "resolution_effectiveness": 40,
            "ownership_next_steps": 20,
            "active_listening": 20,
            "tone_empathy_professionalism": 20,
        }},
    ).status_code == 403
