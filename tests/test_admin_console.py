"""AC-5: platform-admin directory, usage, feature toggles."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.cost_estimate import estimate_usage_cost
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = "00000000-0000-4000-8000-0000000000aa"
ORG_B = "00000000-0000-4000-8000-0000000000bb"

_BODY = {
    "org_id": ORG_A,
    "feature_key": "show_usage_bar",
    "enabled": False,
}


def test_estimate_usage_cost_uses_unit_and_hit_rates(monkeypatch):
    monkeypatch.setenv("COST_PYAI_USD_PER_UNIT", "0.10")
    monkeypatch.setenv("COST_CLAUDE_USD_PER_HIT", "0.05")
    out = estimate_usage_cost(
        {
            "by_provider": {
                "pyai": {"units": 3, "actions": 9, "hits": 9, "polls": 2},
                "anthropic": {"hits": 4, "units": 0, "actions": 4, "polls": 0},
            }
        }
    )
    assert out == {"pyai_usd": 0.3, "claude_usd": 0.2, "total_usd": 0.5}


def test_non_admin_gets_403_on_all_admin_reads_and_writes(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    usage = MagicMock()
    search = MagicMock()
    monkeypatch.setattr("backend.admin_console.pyai_usage.usage_summary", usage)
    monkeypatch.setattr("backend.admin_console.search_directory", search)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get("/api/admin/directory", params={"q": "ada"}).status_code == 403
    assert client.get("/api/admin/usage", params={"org_id": ORG_A}).status_code == 403
    assert client.post("/api/admin/features", json=_BODY).status_code == 403
    assert (
        client.post(
            "/api/admin/log-password-reset-request",
            json={"user_id": str(uuid.uuid4()), "email": "ada@example.com"},
        ).status_code
        == 403
    )
    usage.assert_not_called()


def test_usage_passes_queried_org_not_caller_org(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[str] = []

    def _summary(since_iso=None, *, org_id=None):
        seen.append(org_id or "")
        return {
            "by_provider": {
                "pyai": {"units": 1, "actions": 1, "hits": 1, "polls": 0},
                "anthropic": {"hits": 0, "units": 0, "actions": 0, "polls": 0},
            }
        }

    monkeypatch.setattr("backend.admin_console.pyai_usage.usage_summary", _summary)
    monkeypatch.setattr(
        "backend.admin_console.org_features.features_for_org",
        lambda org_id: {"show_usage_bar": True},
    )
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/usage", params={"org_id": ORG_A})
    assert r.status_code == 200
    assert seen == [ORG_A]
    assert r.json()["org_id"] == ORG_A
    assert r.json()["org_id"] != DEFAULT_ORG_ID
    assert "cost" in r.json()
    assert "pyai_usd" in r.json()["cost"]


def test_toggle_does_not_call_usage_for_other_org(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    wrote: list[tuple] = []

    def _set(org_id, feature_key, enabled, *, changed_by):
        wrote.append((org_id, feature_key, enabled, changed_by))
        return {"show_usage_bar": False}

    monkeypatch.setattr("backend.admin_console.org_features.set_feature", _set)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/admin/features", json=_BODY)
    assert r.status_code == 200
    assert wrote == [(ORG_A, "show_usage_bar", False, "tester@example.com")]


def test_directory_search_is_gated_and_forwards_q(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    monkeypatch.setattr(
        "backend.admin_console.search_directory",
        lambda q: {"rows": [{"email": "ada@x.com", "q": q}]},
    )
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/directory", params={"q": "ada"})
    assert r.status_code == 200
    assert r.json()["rows"][0]["email"] == "ada@x.com"


def test_admin_console_does_not_bypass_rls():
    src = (ROOT / "backend" / "admin_console.py").read_text(encoding="utf-8")
    assert "bypass_rls=True" not in src
    assert "GRANT" not in src
    assert "org_id=oid" in src.replace(" ", "")
    rev = (ROOT / "alembic" / "versions" / "0014_org_features_write.py").read_text(
        encoding="utf-8"
    )
    assert "GRANT SELECT ON org_directory TO callproof_app" not in rev.lower()
    assert "admin_search_directory" in rev
    assert "SECURITY DEFINER" in rev
    assert "CREATE POLICY org_features_insert" in rev
    hist = (ROOT / "alembic" / "versions" / "0016_org_features_history.py").read_text(
        encoding="utf-8"
    )
    assert "bypass_rls" not in hist
    assert "GRANT SELECT, INSERT ON org_features_history TO callproof_app" in hist
    assert "GRANT SELECT ON org_directory TO callproof_app" not in hist.lower()
    assert "CREATE POLICY org_features_history_insert" in hist
    assert "CREATE POLICY org_features_history_update" not in hist
    assert "CREATE POLICY org_features_history_delete" not in hist
    assert uuid.UUID(ORG_A)


def test_log_password_reset_request_writes_audit_not_password(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    uid = str(uuid.uuid4())
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    with caplog.at_level(logging.INFO, logger="callproof.api"):
        r = client.post(
            "/api/admin/log-password-reset-request",
            json={"user_id": uid, "email": "Ada@Example.com"},
        )
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    line = next(
        rec.getMessage()
        for rec in caplog.records
        if "admin_password_reset_email_sent" in rec.getMessage()
    )
    assert f"user_id={uid}" in line
    assert "email=ada@example.com" in line
    assert "admin_email=tester@example.com" in line
    assert "temporary_password" not in line
    assert " password=" not in line


def test_log_password_reset_request_rejects_bad_body(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert (
        client.post(
            "/api/admin/log-password-reset-request",
            json={"user_id": "not-a-uuid", "email": "ada@example.com"},
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/admin/log-password-reset-request",
            json={"user_id": str(uuid.uuid4()), "email": "not-an-email"},
        ).status_code
        == 400
    )


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _DetailFakeConn:
    """Answers exactly the four queries call_detail() issues, org-scoped."""

    def __init__(self, calls, audited_call_ids):
        self.calls = calls
        self.audited_call_ids = audited_call_ids
        self.seen_org_ids: list[str] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        oid = params[0] if params else None
        self.seen_org_ids.append(oid)
        rows_for_org = [c for c in self.calls if c["org_id"] == oid]
        audited_for_org = [a for a in self.audited_call_ids if a[0] == oid]
        if "COUNT(*) AS N FROM CALLS" in norm:
            return _Result([{"n": len(rows_for_org)}])
        if "COUNT(DISTINCT CALL_ID) AS N FROM AUDITS" in norm:
            return _Result([{"n": len({a[1] for a in audited_for_org})}])
        if "SELECT DISTINCT CALL_ID FROM AUDITS" in norm:
            return _Result([{"call_id": a[1]} for a in audited_for_org])
        if "FROM CALLS" in norm and "ORDER BY CREATED_AT" in norm:
            limit = params[1] if len(params) > 1 else len(rows_for_org)
            ordered = sorted(rows_for_org, key=lambda c: c["created_at"], reverse=True)
            return _Result(ordered[:limit])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_detail_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.admin_console.db.connection", _connection)
    yield conn


def test_call_detail_counts_mode_and_audited_are_org_scoped(monkeypatch):
    from backend.admin_console import call_detail

    calls = [
        {"id": 1, "org_id": ORG_A, "filename": "a1.mp3", "job_id": "job_abc",
         "created_at": "2026-01-01", "audio_seconds": 90},
        {"id": 2, "org_id": ORG_A, "filename": "a2.mp3", "job_id": "selfhosted_xyz",
         "created_at": "2026-01-02", "audio_seconds": 200},
        {"id": 3, "org_id": ORG_B, "filename": "b1.mp3", "job_id": "job_other",
         "created_at": "2026-01-03", "audio_seconds": 50},
    ]
    audited = [(ORG_A, 2), (ORG_B, 3)]
    conn = _DetailFakeConn(calls, audited)
    with _fake_detail_db(monkeypatch, conn):
        out = call_detail(ORG_A)

    assert out["org_id"] == ORG_A
    assert out["total_calls"] == 2
    assert out["audited_count"] == 1
    assert out["calls_truncated"] is False
    by_id = {c["call_id"]: c for c in out["calls"]}
    assert by_id[1]["mode"] == "pyai"
    assert by_id[1]["audited"] is False
    assert by_id[2]["mode"] == "selfhosted"
    assert by_id[2]["audited"] is True
    assert 3 not in by_id
    assert all(oid == ORG_A for oid in conn.seen_org_ids)


def test_call_detail_route_is_gated_and_org_scoped(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_EMAILS", "tester@example.com")
    seen: list[tuple] = []

    def _detail(org_id, limit=None):
        seen.append((org_id, limit))
        return {
            "org_id": org_id,
            "total_calls": 2,
            "audited_count": 1,
            "calls": [],
            "calls_truncated": False,
        }

    monkeypatch.setattr("backend.admin_console.call_detail", _detail)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get(f"/api/admin/orgs/{ORG_A}/detail")
    assert r.status_code == 200
    assert seen == [(ORG_A, None)]
    assert r.json()["org_id"] == ORG_A


def test_call_detail_route_403_for_non_admin(monkeypatch):
    monkeypatch.delenv("PLATFORM_ADMIN_EMAILS", raising=False)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    assert client.get(f"/api/admin/orgs/{ORG_A}/detail").status_code == 403


def test_log_password_reset_route_does_not_call_supabase_admin():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def log_password_reset_request")
    end = src.index("# --- end platform admin ---", start)
    region = src[start:end].lower()
    assert "service_role" not in region
    assert "auth/v1/admin" not in region
    assert "resetpassword" not in region
    assert "generate_link" not in region
    assert "httpx" not in region
