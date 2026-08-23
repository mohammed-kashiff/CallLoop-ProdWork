"""CL-4: first Alembic revision has orgs + tenant-scoped calls/segments/audits."""

from __future__ import annotations

from pathlib import Path

from backend.org_ids import DEFAULT_ORG_ID

ROOT = Path(__file__).resolve().parent.parent
REV = ROOT / "alembic" / "versions" / "0001_orgs_calls_segments_audits.py"


def _sql() -> str:
    text = REV.read_text(encoding="utf-8")
    assert "CREATE TABLE" in text
    return text.upper()


def test_revision_file_exists():
    assert REV.is_file()


def test_orgs_table_and_placeholder_row():
    raw = REV.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE ORGS" in sql
    assert "DEFAULT_ORG_ID" in raw
    assert "INSERT INTO ORGS" in sql
    assert DEFAULT_ORG_ID == "00000000-0000-4000-8000-000000000001"


def test_app_tables_exist():
    sql = _sql()
    for name in ("CALLS", "SEGMENTS", "AUDITS"):
        assert f"CREATE TABLE {name}" in sql


def test_org_id_not_null_and_fk_on_every_app_table():
    sql = _sql()
    assert sql.count("ORG_ID UUID NOT NULL REFERENCES ORGS") == 3


def test_org_id_indexes():
    sql = _sql()
    assert "CREATE INDEX IDX_CALLS_ORG_ID ON CALLS (ORG_ID)" in sql
    assert "CREATE INDEX IDX_SEGMENTS_ORG_ID ON SEGMENTS (ORG_ID)" in sql
    assert "CREATE INDEX IDX_AUDITS_ORG_ID ON AUDITS (ORG_ID)" in sql
