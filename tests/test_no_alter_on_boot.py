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


def test_fourth_revision_org_members():
    rev = ROOT / "alembic" / "versions" / "0004_org_members.py"
    assert rev.is_file()
    sql = rev.read_text(encoding="utf-8").upper()
    assert "CREATE TABLE ORG_MEMBERS" in sql
    assert "USER_ID UUID NOT NULL" in sql
    assert "UNIQUE (USER_ID)" in sql
    assert "CHECK (ROLE IN ('OWNER', 'MEMBER'))" in sql
    assert "REFERENCES ORGS" in sql


def test_fifth_revision_enables_rls():
    rev = ROOT / "alembic" / "versions" / "0005_rls.py"
    assert rev.is_file()
    sql = rev.read_text(encoding="utf-8").upper()
    assert "ENABLE ROW LEVEL SECURITY" in sql
    for table in ("ORGS", "CALLS", "SEGMENTS", "AUDITS", "RUBRICS", "API_USAGE"):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        for action in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            assert f"{table}_{action}" in sql
    assert "CALLPROOF_APP" in sql
    assert "NOBYPASSRLS" in sql
    assert "SERVICE_ROLE" in sql
    assert "ALEMBIC" in sql
    assert "GRANT CALLPROOF_APP TO CURRENT_USER" in sql
    assert "FORCE ROW LEVEL SECURITY" not in sql


def test_sixth_revision_grants_app_role_to_login():
    rev = ROOT / "alembic" / "versions" / "0006_rls_role_grant.py"
    assert rev.is_file()
    sql = rev.read_text(encoding="utf-8").upper()
    assert "GRANT CALLPROOF_APP TO CURRENT_USER" in sql
    assert "0005_RLS" in sql


def test_seventh_revision_disables_rls_on_org_members():
    rev = ROOT / "alembic" / "versions" / "0007_org_members_no_rls.py"
    assert rev.is_file()
    sql = rev.read_text(encoding="utf-8").upper()
    assert "ALTER TABLE ORG_MEMBERS DISABLE ROW LEVEL SECURITY" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql  # downgrade only

