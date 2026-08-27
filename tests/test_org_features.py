"""AC-4: org_features defaults on; /api/me overlays rows; SELECT-only RLS."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_features import FEATURE_KEYS, features_for_org
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

OTHER_ORG = "00000000-0000-4000-8000-0000000000bb"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        norm = self.sql[-1].upper()
        if "FROM ORG_FEATURES" in norm:
            oid = params[0] if params else None
            return _Result([r for r in self.rows if r.get("org_id") == oid])
        return _Result([])


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.org_features.db.connection", _cm)
    yield conn


def test_missing_row_is_enabled(monkeypatch):
    conn = _FakeConn(rows=[])
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags == {key: True for key in FEATURE_KEYS}
    assert any("org_id = %s" in s for s in conn.sql)


def test_disabled_row_is_reported_false(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": DEFAULT_ORG_ID,
                "feature_key": "usage_bar",
                "enabled": False,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["usage_bar"] is False
    assert flags["secondary_nav"] is True
    assert flags["powered_by_badge"] is True
    assert flags["billed_usage_panel"] is True


def test_unknown_db_key_is_included_without_schema_change(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": DEFAULT_ORG_ID,
                "feature_key": "future_flag",
                "enabled": False,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["future_flag"] is False
    assert flags["usage_bar"] is True


def test_other_org_rows_are_not_applied(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": OTHER_ORG,
                "feature_key": "usage_bar",
                "enabled": False,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["usage_bar"] is True


def test_me_reports_disabled_flag(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.org_features.features_for_org",
        lambda org_id: {
            "usage_bar": False,
            "secondary_nav": True,
            "powered_by_badge": True,
            "billed_usage_panel": True,
        },
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["features"]["usage_bar"] is False
    assert body["org_id"] == DEFAULT_ORG_ID
    assert body["role"] == "owner"


def test_org_features_module_does_not_bypass_or_touch_directory():
    src = (ROOT / "backend" / "org_features.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "org_directory" not in src
    api = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    assert "org_directory" not in api
