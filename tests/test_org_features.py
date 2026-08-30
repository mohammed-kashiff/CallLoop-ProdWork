"""AC-4/AC-5: org_features defaults on; /api/me overlays rows."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_features import FEATURE_KEYS, default_features, features_for_org, set_feature
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
        self.inserts: list = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        norm = self.sql[-1].upper()
        if "INSERT INTO ORG_FEATURES" in norm:
            self.inserts.append(params)
            oid, key, enabled = params[0], params[1], params[2]
            self.rows = [
                r
                for r in self.rows
                if not (r.get("org_id") == oid and r.get("feature_key") == key)
            ]
            self.rows.append({"org_id": oid, "feature_key": key, "enabled": enabled})
            return _Result([])
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
    assert flags["show_usage_bar"] is True
    assert flags["show_neighbourhood_nav"] is True
    assert flags["show_growth_tools_nav"] is True
    assert flags["show_powered_by_pyai"] is True
    assert flags["show_billed_usage_panel"] is True
    assert flags["use_selfhosted_transcription"] is False
    assert any("org_id = %s" in s for s in conn.sql)


def test_selfhosted_flag_is_off_until_row_exists(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": DEFAULT_ORG_ID,
                "feature_key": "use_selfhosted_transcription",
                "enabled": True,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["use_selfhosted_transcription"] is True
    assert flags["show_usage_bar"] is True


def test_default_features_keeps_trial_on_and_selfhosted_off():
    flags = default_features()
    assert "use_selfhosted_transcription" in FEATURE_KEYS
    assert flags["use_selfhosted_transcription"] is False
    assert all(
        flags[key] is True
        for key in FEATURE_KEYS
        if key != "use_selfhosted_transcription"
    )


def test_disabled_row_is_reported_false(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": DEFAULT_ORG_ID,
                "feature_key": "show_usage_bar",
                "enabled": False,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["show_usage_bar"] is False
    assert flags["show_neighbourhood_nav"] is True
    assert flags["show_powered_by_pyai"] is True
    assert flags["show_billed_usage_panel"] is True


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
    assert flags["show_usage_bar"] is True


def test_other_org_rows_are_not_applied(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": OTHER_ORG,
                "feature_key": "show_usage_bar",
                "enabled": False,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = features_for_org(DEFAULT_ORG_ID)
    assert flags["show_usage_bar"] is True


def test_upsert_writes_one_row_for_target_org_only(monkeypatch):
    conn = _FakeConn(
        rows=[
            {
                "org_id": OTHER_ORG,
                "feature_key": "show_usage_bar",
                "enabled": True,
            }
        ]
    )
    with _fake_db(monkeypatch, conn):
        flags = set_feature(DEFAULT_ORG_ID, "show_usage_bar", False)
    assert len(conn.inserts) == 1
    assert conn.inserts[0][0] == DEFAULT_ORG_ID
    assert conn.inserts[0][1] == "show_usage_bar"
    assert conn.inserts[0][2] is False
    assert flags["show_usage_bar"] is False
    other = [r for r in conn.rows if r["org_id"] == OTHER_ORG]
    assert other == [
        {
            "org_id": OTHER_ORG,
            "feature_key": "show_usage_bar",
            "enabled": True,
        }
    ]


def test_me_reports_disabled_flag(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.org_features.features_for_org",
        lambda org_id: {
            "show_usage_bar": False,
            "show_neighbourhood_nav": True,
            "show_growth_tools_nav": True,
            "show_powered_by_pyai": True,
            "show_billed_usage_panel": True,
        },
    )
    r = client.get("/api/me")
    assert r.status_code == 200
    body = r.json()
    assert body["features"]["show_usage_bar"] is False
    assert body["org_id"] == DEFAULT_ORG_ID
    assert body["role"] == "owner"


def test_org_features_module_does_not_bypass_or_touch_directory():
    src = (ROOT / "backend" / "org_features.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "org_directory" not in src
    api = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    assert "org_directory" not in api
