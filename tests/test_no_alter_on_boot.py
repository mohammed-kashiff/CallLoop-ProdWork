"""CL-5: schema changes go through Alembic, not ALTER TABLE at process start."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"


def test_no_alter_table_in_backend():
    hits: list[str] = []
    for path in BACKEND.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if "ALTER TABLE" in line.upper() and not line.lstrip().startswith("#"):
                hits.append(f"{path.name}:{i}")
    assert hits == [], "move schema changes to alembic/versions/: " + ", ".join(hits)


def test_second_revision_exists():
    rev = ROOT / "alembic" / "versions" / "0002_api_usage.py"
    assert rev.is_file()
    sql = rev.read_text(encoding="utf-8").upper()
    assert "CREATE TABLE API_USAGE" in sql
    assert "ORG_ID UUID NOT NULL REFERENCES ORGS" in sql


def test_third_revision_recreates_audits_with_rubric_key():
    rev = ROOT / "alembic" / "versions" / "0003_rubrics_audits.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE RUBRICS" in sql
    assert "ORG_ID UUID NOT NULL REFERENCES ORGS" in sql
    assert "UNIQUE (CALL_ID, RUBRIC_ID, RUBRIC_VERSION)" in sql
    assert "PRIMARY KEY (CALL_ID, RUBRIC_ID)" not in sql
    assert "LEGACY_RUBRIC_NAME" in raw
    assert "load_v8_definition" in raw
    assert "DROP TABLE IF EXISTS AUDITS" in sql
    assert "INSERT INTO AUDITS" not in sql  # no backfill

