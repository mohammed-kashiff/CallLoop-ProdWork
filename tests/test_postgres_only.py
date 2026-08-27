"""Postgres is required. SQLite paths, imports, and Render disks must stay gone."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.db import require_database_url

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
ALEMBIC_VERSIONS = ROOT / "alembic" / "versions"
RENDER_YAML = ROOT / "render.yaml"
CUTOVER_DOC = ROOT / "docs" / "postgres-cutover.md"


def test_paths_has_no_sqlite_file():
    import backend.paths as paths

    assert not hasattr(paths, "DB_PATH")
    text = (BACKEND / "paths.py").read_text(encoding="utf-8")
    assert "callproof.db" not in text


def test_backend_does_not_import_sqlite3():
    hits: list[str] = []
    for path in BACKEND.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3" or alias.name.startswith("sqlite3."):
                        hits.append(f"{path.name}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("sqlite3"):
                hits.append(f"{path.name}:{node.lineno}")
    assert hits == [], hits


def test_backend_does_not_open_sqlite_file():
    hits: list[str] = []
    for path in BACKEND.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "callproof.db" in line and not line.lstrip().startswith("#"):
                hits.append(f"{path.relative_to(ROOT)}:{i}")
    assert hits == [], hits


def test_require_database_url_has_no_sqlite_fallback(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    with pytest.raises(RuntimeError, match="no SQLite fallback"):
        require_database_url()


def test_render_yaml_has_no_volume_disk():
    text = RENDER_YAML.read_text(encoding="utf-8")
    assert "\ndisks:" not in text
    assert not text.startswith("disks:")
    assert "DATABASE_URL" in text
    assert "alembic upgrade head && uvicorn backend.api:app" in text


def test_ci_practises_alembic_downgrade_on_clone():
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "alembic downgrade -1" in text
    assert "alembic upgrade head" in text


def test_every_alembic_revision_has_downgrade():
    files = sorted(ALEMBIC_VERSIONS.glob("*.py"))
    assert files, "no alembic versions"
    missing: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        names = {
            node.name
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
        }
        if "upgrade" not in names or "downgrade" not in names:
            missing.append(path.name)
    assert missing == [], missing


def test_cutover_doc_covers_rollback_and_local_dev():
    assert CUTOVER_DOC.is_file()
    text = CUTOVER_DOC.read_text(encoding="utf-8").lower()
    for needle in (
        "no sqlite fallback",
        "alembic upgrade head",
        "alembic downgrade",
        "do not run it against production",
        "point-in-time",
        "previous git sha",
        "disks:",
        "local development",
    ):
        assert needle in text, needle
