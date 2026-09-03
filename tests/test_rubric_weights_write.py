"""CR-13: platform-admin write path for per-org rubric weights (FR2)."""

from __future__ import annotations

import copy
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.audit_store import LEGACY_RUBRIC_NAME, insert_weighted_version, load_v8_definition
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = "00000000-0000-4000-8000-0000000000aa"
ORG_B = "00000000-0000-4000-8000-0000000000bb"

DEFAULT_WEIGHTS = {
    "resolution_effectiveness": 40,
    "ownership_next_steps": 20,
    "active_listening": 20,
    "tone_empathy_professionalism": 20,
}
CUSTOM_WEIGHTS = {
    "resolution_effectiveness": 50,
    "ownership_next_steps": 20,
    "active_listening": 15,
    "tone_empathy_professionalism": 15,
}
BAD_SUM = {
    "resolution_effectiveness": 41,
    "ownership_next_steps": 20,
    "active_listening": 20,
    "tone_empathy_professionalism": 20,
}


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def _unwrap(value):
    return getattr(value, "obj", value)


class _TxConn:
    """In-memory rubrics table with commit/rollback so a failed insert undoes deactivate."""

    def __init__(self, rows=None, *, fail_on_insert=False):
        self.committed = copy.deepcopy(rows or [])
        self.rows = copy.deepcopy(rows or [])
        self.fail_on_insert = fail_on_insert
        self.sql: list[str] = []

    def commit(self):
        self.committed = copy.deepcopy(self.rows)

    def rollback(self):
        self.rows = copy.deepcopy(self.committed)

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.sql.append(norm)
        oid = params[0] if params else None
        if "FROM RUBRICS" in norm and "FOR UPDATE" in norm:
            active = [
                r for r in self.rows
                if r["org_id"] == oid and r["is_active"]
            ]
            active.sort(key=lambda r: r["version"], reverse=True)
            return _Result(active[:1])
        if "MAX(VERSION)" in norm:
            name = params[1]
            versions = [
                r["version"] for r in self.rows
                if r["org_id"] == oid and r["name"] == name
            ]
            return _Result([{"v": max(versions) if versions else 0}])
        if "UPDATE RUBRICS" in norm and "IS_ACTIVE = FALSE" in norm:
            for row in self.rows:
                if row["org_id"] == oid and row["is_active"]:
                    row["is_active"] = False
            return _Result([])
        if "INSERT INTO RUBRICS" in norm:
            if self.fail_on_insert:
                raise RuntimeError("forced mid-write failure")
            row = {
                "id": params[0],
                "org_id": params[1],
                "name": params[2],
                "version": params[3],
                "definition": _unwrap(params[4]),
                "is_active": True,
                "updated_at": datetime(2026, 3, 1, tzinfo=timezone.utc),
            }
            self.rows.append(row)
            return _Result([row])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _tx_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    monkeypatch.setattr("backend.admin_console.db.connection", _connection)
    yield conn


def _active(conn, org_id=ORG_A):
    return [r for r in conn.rows if r["org_id"] == org_id and r["is_active"]]


def _seed_row(org_id=ORG_A, *, name=LEGACY_RUBRIC_NAME, version=1, weights=None):
    definition = load_v8_definition()
    if weights:
        from backend.audit_store import apply_dimension_weights
        definition = apply_dimension_weights(definition, weights)
    return {
        "id": str(uuid.uuid4()),
        "org_id": org_id,
        "name": name,
        "version": version,
        "definition": definition,
        "is_active": True,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
    }


def test_weights_not_summing_to_100_writes_nothing(monkeypatch):
    from backend.admin_console import save_org_rubric

    conn = _TxConn([_seed_row()])
    with _tx_db(monkeypatch, conn):
        with pytest.raises(HTTPException) as exc:
            save_org_rubric(ORG_A, BAD_SUM)
    assert exc.value.status_code == 400
    assert "100" in str(exc.value.detail)
    assert conn.sql == []
    assert len(_active(conn)) == 1
    assert _active(conn)[0]["version"] == 1


def test_mid_write_failure_leaves_exactly_one_active(monkeypatch):
    from backend.admin_console import save_org_rubric

    seed = _seed_row()
    conn = _TxConn([seed], fail_on_insert=True)
    with _tx_db(monkeypatch, conn):
        with pytest.raises(RuntimeError, match="forced mid-write failure"):
            save_org_rubric(ORG_A, CUSTOM_WEIGHTS)
    active = _active(conn)
    assert len(active) == 1
    assert active[0]["id"] == seed["id"]
    assert active[0]["version"] == 1
    assert sum(1 for r in conn.rows if r["org_id"] == ORG_A and r["is_active"]) == 1


def test_two_sequential_saves_never_leave_two_active_and_reuse_name(monkeypatch):
    from backend.admin_console import save_org_rubric

    seed = _seed_row(name="Keep this name")
    conn = _TxConn([seed])
    with _tx_db(monkeypatch, conn):
        first = save_org_rubric(ORG_A, CUSTOM_WEIGHTS)
        second = save_org_rubric(
            ORG_A,
            {
                "resolution_effectiveness": 10,
                "ownership_next_steps": 30,
                "active_listening": 30,
                "tone_empathy_professionalism": 30,
            },
        )
    names = {r["name"] for r in conn.rows if r["org_id"] == ORG_A}
    assert names == {"Keep this name"}
    assert len(_active(conn)) == 1
    assert _active(conn)[0]["id"] == second["rubric_id"]
    assert first["version"] == 2
    assert second["version"] == 3
    assert first["rubric_id"] != seed["id"]
    assert second["rubric_id"] != first["rubric_id"]
    inactive = [r for r in conn.rows if r["org_id"] == ORG_A and not r["is_active"]]
    assert len(inactive) == 2
    assert {r["version"] for r in inactive} == {1, 2}


def test_save_does_not_invent_a_name_that_would_dodge_the_unique_index():
    """uq_rubrics_org_name_active is (org_id, name) WHERE is_active.
    A new name per save would allow two active rows for one org."""
    src = (ROOT / "backend" / "audit_store.py").read_text(encoding="utf-8")
    start = src.index("def insert_weighted_version")
    end = src.index("\ndef fetch_latest_for_rubric", start)
    region = src[start:end]
    assert "LEGACY_RUBRIC_NAME" in region
    assert 'active["name"]' in region
    assert "SET definition" not in region.replace(" ", "").lower()
    assert "INSERT INTO rubrics" in region
    assert "is_active = false" in region.lower()


def test_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        f"/api/admin/orgs/{DEFAULT_ORG_ID}/rubric",
        json={"weights": DEFAULT_WEIGHTS},
    )
    assert r.status_code == 403


def test_route_is_gated_and_forwards_org(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[tuple] = []

    def _save(org_id, weights):
        seen.append((org_id, dict(weights)))
        return {
            "org_id": org_id, "source": "custom", "rubric_id": "rid",
            "version": 2, "updated_at": None, "weights": weights,
        }

    monkeypatch.setattr("backend.api.admin_console.save_org_rubric", _save)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        f"/api/admin/orgs/{ORG_A}/rubric",
        json={"weights": CUSTOM_WEIGHTS},
    )
    assert r.status_code == 200
    assert seen == [(ORG_A, CUSTOM_WEIGHTS)]
    assert r.json()["org_id"] == ORG_A


def test_insert_weighted_version_does_not_touch_another_org():
    other = _seed_row(ORG_B, name="Other org", version=1)
    conn = _TxConn([_seed_row(), other])
    out = insert_weighted_version(conn, org_id=ORG_A, weights=CUSTOM_WEIGHTS)
    assert out["version"] == 2
    assert len(_active(conn, ORG_B)) == 1
    assert _active(conn, ORG_B)[0]["id"] == other["id"]
    assert _active(conn, ORG_B)[0]["name"] == "Other org"


def test_first_save_for_org_with_no_row_uses_legacy_name():
    conn = _TxConn([])
    out = insert_weighted_version(conn, org_id=ORG_A, weights=CUSTOM_WEIGHTS)
    assert out["name"] == LEGACY_RUBRIC_NAME
    assert out["version"] == 1
    assert len(_active(conn)) == 1
    assert _active(conn)[0]["definition"]["technical_skills"]["dimensions"][0]["weight"] == 50


def test_live_unique_index_allows_two_actives_if_names_differ_and_save_does_not():
    """Schema trap + our write path. Skips when DATABASE_URL is unset."""
    from dotenv import load_dotenv
    from psycopg.rows import dict_row
    from psycopg.types.json import Json

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    org_id = str(uuid.uuid4())
    defn = load_v8_definition()
    conn = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    conn.autocommit = False
    try:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "cr13-rubric"))
        conn.execute(
            """
            INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
            VALUES (%s, %s, %s, 1, %s, true), (%s, %s, %s, 1, %s, true)
            """,
            (
                str(uuid.uuid4()), org_id, "Name A", Json(defn),
                str(uuid.uuid4()), org_id, "Name B", Json(defn),
            ),
        )
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM rubrics WHERE org_id = %s AND is_active",
            (org_id,),
        ).fetchone()["n"]
        assert int(n) == 2

        conn.execute("SAVEPOINT after_trap")
        saved = insert_weighted_version(conn, org_id=org_id, weights=CUSTOM_WEIGHTS)
        rows = conn.execute(
            """
            SELECT name, version, is_active FROM rubrics
            WHERE org_id = %s ORDER BY version, name
            """,
            (org_id,),
        ).fetchall()
        active = [r for r in rows if r["is_active"]]
        assert len(active) == 1
        assert active[0]["name"] == saved["name"]
        assert saved["name"] in {"Name A", "Name B"}
        assert all(r["name"] == saved["name"] or not r["is_active"] for r in rows)

        conn.execute("ROLLBACK TO SAVEPOINT after_trap")

        class _BoomConn:
            def execute(self, sql, params=None):
                if "insert into rubrics" in " ".join(str(sql).split()).lower():
                    raise RuntimeError("forced mid-write failure")
                return conn.execute(sql, params)

        with pytest.raises(RuntimeError, match="forced mid-write failure"):
            insert_weighted_version(_BoomConn(), org_id=org_id, weights=CUSTOM_WEIGHTS)
        conn.execute("ROLLBACK TO SAVEPOINT after_trap")
        n_after = conn.execute(
            "SELECT COUNT(*) AS n FROM rubrics WHERE org_id = %s AND is_active",
            (org_id,),
        ).fetchone()["n"]
        assert int(n_after) == 2
    finally:
        conn.rollback()
        conn.close()
