"""Safety net: boot, routes, paths, and rubric shape. No live API keys required."""

from __future__ import annotations

import json
from pathlib import Path

from backend.config import cors_origins
from backend.paths import AUDIO_DIR, DB_PATH, ENV_FILE, LOG_FILE, ROOT, RUBRIC_PATH


EXPECTED_ROUTES = {
    "/",
    "/health",
    "/healthz",
    "/api/pyai/status",
    "/api/keys",
    "/api/dev/logs",
    "/api/calls",
    "/api/cache/clear",
    "/api/calls/flagged",
    "/api/calls/export-scorecard",
    "/api/calls/export",
    "/api/calls/{call_id}/audit",
    "/api/calls/{call_id}/flag",
    "/api/calls/{call_id}/solve",
    "/api/calls/{call_id}/feedback",
    "/api/calls/{call_id}/stakeholder-email/compose",
    "/api/calls/{call_id}/audio",
    "/api/upload",
    "/api/upload-batch",
    "/api/integrations/justcall",
    "/api/integrations/justcall/sync",
    "/api/integrations/justcall/webhook",
}

AUDIT_CONTRACT_KEYS = {
    "call_id",
    "score",
    "grade",
    "filename",
    "flagged",
    "review_solved",
    "churn",
}


def test_repo_paths_are_under_repo_root():
    assert (ROOT / "backend" / "api.py").is_file()
    assert Path(DB_PATH) == ROOT / "callproof.db"
    assert Path(AUDIO_DIR) == ROOT / "audio"
    assert Path(LOG_FILE) == ROOT / "logs" / "callproof.log"
    assert Path(RUBRIC_PATH) == ROOT / "rubric.json"
    assert Path(ENV_FILE) == ROOT / ".env"


def test_paths_stable_if_cwd_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from backend.paths import DB_PATH as db_again
    from backend.paths import RUBRIC_PATH as rubric_again
    from backend.paths import ROOT as root_again

    assert root_again == ROOT
    assert Path(db_again) == ROOT / "callproof.db"
    assert Path(rubric_again).is_file()


def test_runtime_rubric_is_v8_shaped():
    data = json.loads(Path(RUBRIC_PATH).read_text(encoding="utf-8"))
    assert data.get("rubric_id")
    assert "technical_skills" in data
    assert "soft_skills" in data
    assert (data.get("technical_skills") or {}).get("dimensions")
    assert (data.get("soft_skills") or {}).get("dimensions")


def test_app_imports_and_exposes_current_routes():
    from backend.api import app
    from api import app as shim_app

    assert shim_app is app
    paths = {getattr(route, "path", None) for route in app.routes}
    missing = EXPECTED_ROUTES - paths
    assert not missing, f"missing routes: {sorted(missing)}"


def test_health_returns_200_without_external_calls():
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    for path in ("/health", "/healthz", "/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.json() == {"ok": True}


def test_cors_defaults_include_vite_dev_ports(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    origins = cors_origins()
    assert "http://localhost:5173" in origins
    assert "http://127.0.0.1:5173" in origins
    assert "http://localhost:5174" in origins


def test_cors_origins_from_env(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://app.example")
    assert cors_origins() == ["https://app.example"]


def test_audit_mapper_still_reads_known_fields():
    """Frontend mapAudit.ts depends on these keys staying in audit JSON."""
    mapper = Path(ROOT / "frontend" / "src" / "lib" / "mapAudit.ts").read_text(encoding="utf-8")
    for key in AUDIT_CONTRACT_KEYS:
        assert key in mapper, f"mapAudit.ts no longer mentions {key}"
