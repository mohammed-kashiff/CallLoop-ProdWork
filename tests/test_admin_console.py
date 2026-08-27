"""AC-5: platform-admin directory, usage, feature toggles."""

from __future__ import annotations

import uuid
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

    def _set(org_id, feature_key, enabled):
        wrote.append((org_id, feature_key, enabled))
        return {"show_usage_bar": False}

    monkeypatch.setattr("backend.admin_console.org_features.set_feature", _set)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/admin/features", json=_BODY)
    assert r.status_code == 200
    assert wrote == [(ORG_A, "show_usage_bar", False)]


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
    assert uuid.UUID(ORG_A)
