"""Multi-rubric library: teams save several named rubrics, one active at a
time, switchable — built on the same rubrics table, no migration."""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID
ORG_B = "00000000-0000-4000-8000-0000000000bb"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _unwrap(value):
    return getattr(value, "obj", value)


class _LibraryConn:
    """In-memory rubrics table implementing exactly the queries
    list_rubric_lineages/fetch_rubric_by_name/save_named_rubric/
    activate_rubric_by_name issue."""

    def __init__(self, rows=None):
        self.rows = copy.deepcopy(rows or [])

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        params = params or []

        if norm == "SELECT NAME FROM RUBRICS WHERE ORG_ID = %S AND IS_ACTIVE LIMIT 1":
            org_id = params[0]
            active = [r for r in self.rows if r["org_id"] == org_id and r["is_active"]]
            return _Result([{"name": active[0]["name"]}] if active else [])

        if "DISTINCT ON (NAME)" in norm:
            org_id = params[0]
            by_name: dict[str, dict] = {}
            for r in self.rows:
                if r["org_id"] != org_id:
                    continue
                cur = by_name.get(r["name"])
                if cur is None or r["version"] > cur["version"]:
                    by_name[r["name"]] = r
            ordered = sorted(by_name.values(), key=lambda r: r["name"])
            return _Result(ordered)

        if "COALESCE(MAX(VERSION), 0)" in norm:
            org_id, name = params
            versions = [r["version"] for r in self.rows if r["org_id"] == org_id and r["name"] == name]
            return _Result([{"v": max(versions) if versions else 0}])

        if norm.startswith("SELECT ID, NAME, VERSION, DEFINITION, IS_ACTIVE, UPDATED_AT"):
            org_id, name = params
            matches = [r for r in self.rows if r["org_id"] == org_id and r["name"] == name]
            matches.sort(key=lambda r: r["version"], reverse=True)
            return _Result(matches[:1])

        if norm.startswith("SELECT ID, VERSION, DEFINITION, UPDATED_AT") and "FOR UPDATE" in norm:
            org_id, name = params
            matches = [r for r in self.rows if r["org_id"] == org_id and r["name"] == name]
            matches.sort(key=lambda r: r["version"], reverse=True)
            return _Result(matches[:1])

        if "UPDATE RUBRICS" in norm and "IS_ACTIVE = FALSE" in norm and "WHERE ID = %S" not in norm:
            org_id = params[0]
            for r in self.rows:
                if r["org_id"] == org_id and r["is_active"]:
                    r["is_active"] = False
            return _Result([])

        if "UPDATE RUBRICS" in norm and "IS_ACTIVE = TRUE" in norm:
            rid = params[0]
            for r in self.rows:
                if r["id"] == rid:
                    r["is_active"] = True
            return _Result([])

        if norm.startswith("INSERT INTO RUBRICS"):
            rid, org_id, name, version, definition, is_active = params
            row = {
                "id": rid, "org_id": org_id, "name": name, "version": version,
                "definition": _unwrap(definition), "is_active": bool(is_active),
                "updated_at": datetime(2026, 5, 1, tzinfo=timezone.utc),
            }
            self.rows.append(row)
            return _Result([row])

        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.rubric_builder.db.connection", _connection)
    yield conn


def _seed(org_id, name, *, version=1, active=True):
    from backend.audit_store import load_v8_definition

    return {
        "id": str(uuid.uuid4()), "org_id": org_id, "name": name, "version": version,
        "definition": load_v8_definition(), "is_active": active,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


BUILTIN_100 = [{"kind": "builtin", "id": "resolution_effectiveness", "weight": 100}]


# ---------- audit_store level ----------


def test_save_named_rubric_does_not_touch_other_names_when_not_activating():
    from backend.audit_store import apply_dimension_weights, load_v8_definition, save_named_rubric

    conn = _LibraryConn([_seed(ORG_A, "Sales calls")])
    definition = apply_dimension_weights(
        load_v8_definition(), {"resolution_effectiveness": 100, "ownership_next_steps": 0,
                                "active_listening": 0, "tone_empathy_professionalism": 0},
    )
    out = save_named_rubric(conn, org_id=ORG_A, name="Support calls", definition=definition, activate=False)
    assert out["is_active"] is False
    assert out["version"] == 1
    active = [r for r in conn.rows if r["org_id"] == ORG_A and r["is_active"]]
    assert len(active) == 1
    assert active[0]["name"] == "Sales calls"


def test_save_named_rubric_activating_deactivates_every_other_name():
    from backend.audit_store import apply_dimension_weights, load_v8_definition, save_named_rubric

    conn = _LibraryConn([_seed(ORG_A, "Sales calls"), _seed(ORG_A, "Support calls", active=False)])
    definition = apply_dimension_weights(
        load_v8_definition(), {"resolution_effectiveness": 100, "ownership_next_steps": 0,
                                "active_listening": 0, "tone_empathy_professionalism": 0},
    )
    save_named_rubric(conn, org_id=ORG_A, name="Support calls", definition=definition, activate=True)
    active = [r for r in conn.rows if r["org_id"] == ORG_A and r["is_active"]]
    assert len(active) == 1
    assert active[0]["name"] == "Support calls"
    assert active[0]["version"] == 2


def test_activate_rubric_by_name_switches_without_new_version():
    from backend.audit_store import activate_rubric_by_name

    conn = _LibraryConn([_seed(ORG_A, "Sales calls"), _seed(ORG_A, "Support calls", active=False)])
    out = activate_rubric_by_name(conn, org_id=ORG_A, name="Support calls")
    assert out["version"] == 1  # no new version — just a flag flip
    active = [r for r in conn.rows if r["org_id"] == ORG_A and r["is_active"]]
    assert len(active) == 1
    assert active[0]["name"] == "Support calls"


def test_activate_rubric_by_name_unknown_name_raises_value_error():
    import pytest

    from backend.audit_store import activate_rubric_by_name

    conn = _LibraryConn([_seed(ORG_A, "Sales calls")])
    with pytest.raises(ValueError):
        activate_rubric_by_name(conn, org_id=ORG_A, name="Nonexistent")


def test_list_rubric_lineages_scoped_per_org_latest_version_only():
    from backend.audit_store import list_rubric_lineages

    conn = _LibraryConn([
        _seed(ORG_A, "Sales calls", version=1, active=False),
        _seed(ORG_A, "Sales calls", version=2, active=True),
        _seed(ORG_A, "Support calls", version=1, active=False),
        _seed(ORG_B, "Other org's rubric", version=1, active=True),
    ])
    out = list_rubric_lineages(conn, org_id=ORG_A)
    assert {r["name"] for r in out} == {"Sales calls", "Support calls"}
    sales = next(r for r in out if r["name"] == "Sales calls")
    assert sales["version"] == 2
    assert sales["is_active"] is True


# ---------- rubric_builder level ----------


def test_save_rubric_with_explicit_name_creates_a_new_lineage(monkeypatch):
    from backend.rubric_builder import save_rubric

    conn = _LibraryConn([_seed(ORG_A, "Sales calls")])
    with _fake_db(monkeypatch, conn):
        out = save_rubric(
            ORG_A, BUILTIN_100, changed_by="owner@example.com",
            name="Support calls", activate=False,
        )
    assert out["name"] == "Support calls"
    assert out["is_active"] is False
    names = {r["name"] for r in conn.rows if r["org_id"] == ORG_A}
    assert names == {"Sales calls", "Support calls"}


def test_save_rubric_no_name_reuses_the_currently_active_lineage(monkeypatch):
    from backend.rubric_builder import save_rubric

    conn = _LibraryConn([_seed(ORG_A, "Sales calls")])
    with _fake_db(monkeypatch, conn):
        out = save_rubric(ORG_A, BUILTIN_100, changed_by="owner@example.com")
    assert out["name"] == "Sales calls"
    assert out["version"] == 2


def test_list_rubrics_route_requires_no_special_role(monkeypatch):
    from backend.api import app

    def _list(org_id):
        return {"org_id": org_id, "rubrics": [{"name": "Sales calls", "is_active": True}]}

    monkeypatch.setattr("backend.api.rubric_builder.list_rubrics", _list)
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/rubrics")
    assert r.status_code == 200
    assert r.json()["rubrics"][0]["name"] == "Sales calls"


def test_get_rubric_by_name_route_404_for_unknown_name(monkeypatch):
    from backend.api import app

    conn = _LibraryConn([_seed(ORG_A, "Sales calls")])
    with _fake_db(monkeypatch, conn):
        client = TestClient(app)
        authorize(client, monkeypatch, org_id=ORG_A)
        r = client.get("/api/rubrics/Nonexistent")
    assert r.status_code == 404


def test_activate_rubric_route_owner_only(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    monkeypatch.setattr(
        "backend.api.rubric_builder.activate_rubric",
        lambda org_id, name, *, changed_by: {"org_id": org_id, "name": name, "changed_by": changed_by},
    )
    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(ORG_A, "member", str(user_id)),
    )
    from tests.conftest import mint_access_token

    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.post("/api/rubrics/Sales%20calls/activate")
    assert r.status_code == 403


def test_activate_rubric_route_forwards_name_and_actor(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "")
    seen = []

    def _activate(org_id, name, *, changed_by):
        seen.append((org_id, name, changed_by))
        return {"org_id": org_id, "name": name, "source": "custom", "rubric_id": "r1",
                "version": 3, "is_active": True, "updated_at": None, "dimensions": [],
                "available_builtins": []}

    monkeypatch.setattr("backend.api.rubric_builder.activate_rubric", _activate)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)  # authorize() grants "owner"
    r = client.post("/api/rubrics/Sales%20calls/activate")
    assert r.status_code == 200
    assert seen == [(ORG_A, "Sales calls", "tester@example.com")]
