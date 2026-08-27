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


def test_eighth_revision_private_storage_bucket():
    rev = ROOT / "alembic" / "versions" / "0008_storage_audio_bucket.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CALL-AUDIO" in sql
    assert "PUBLIC" in sql
    assert "FALSE" in sql
    assert "TO_REGCLASS('STORAGE.BUCKETS')" in sql
    assert "call-audio" in raw
    assert "0007_org_members_no_rls" in raw


def test_ninth_revision_org_vault_justcall():
    rev = ROOT / "alembic" / "versions" / "0009_org_vault_justcall.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE ORG_CREDENTIALS" in sql
    assert "KEY_SUFFIX" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "0008_storage_audio_bucket" in raw
    assert "API_KEY" not in sql
    assert "API_SECRET" not in sql


def test_eleventh_revision_names_and_directory_view():
    rev = ROOT / "alembic" / "versions" / "0011_org_members_names_and_directory_view.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "ADD COLUMN FIRST_NAME TEXT" in sql
    assert "ADD COLUMN LAST_NAME TEXT" in sql
    assert "CREATE VIEW ORG_DIRECTORY" in sql
    assert "JOIN AUTH.USERS" in sql
    assert "REVOKE ALL ON ORG_DIRECTORY FROM CALLPROOF_APP" in sql
    assert "0010_orgs_domain" in raw


def test_thirteenth_revision_org_features_select_only():
    rev = ROOT / "alembic" / "versions" / "0013_org_features.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE ORG_FEATURES" in sql
    assert "PRIMARY KEY (ORG_ID, FEATURE_KEY)" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON ORG_FEATURES TO CALLPROOF_APP" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY ORG_FEATURES_SELECT" in sql
    assert "CREATE POLICY ORG_FEATURES_INSERT" not in sql
    assert "CREATE POLICY ORG_FEATURES_UPDATE" not in sql
    assert "CREATE POLICY ORG_FEATURES_DELETE" not in sql
    assert "U.CREATED_AT AS FIRST_SEEN" in sql
    assert "U.LAST_SIGN_IN_AT" in sql
    assert "REVOKE ALL ON ORG_DIRECTORY FROM CALLPROOF_APP" in sql
    assert "GRANT SELECT ON ORG_DIRECTORY TO CALLPROOF_APP" not in sql
    assert "0012_org_members_short_id" in raw
    assert "bypass_rls" not in raw


def test_fourteenth_revision_write_policies_and_directory_fn():
    rev = ROOT / "alembic" / "versions" / "0014_org_features_write.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE POLICY ORG_FEATURES_INSERT" in sql
    assert "CREATE POLICY ORG_FEATURES_UPDATE" in sql
    assert "ADMIN_SEARCH_DIRECTORY" in sql
    assert "SECURITY DEFINER" in sql
    assert "GRANT SELECT ON ORG_DIRECTORY TO CALLPROOF_APP" not in sql
    assert "0013_org_features" in raw


def test_fifteenth_revision_org_id_for_name_excludes_default_org():
    rev = ROOT / "alembic" / "versions" / "0015_org_id_for_name.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE FUNCTION PUBLIC.ORG_ID_FOR_NAME" in sql
    assert "SECURITY DEFINER" in sql
    assert "LOWER(BTRIM(NAME))" in sql
    assert "ORDER BY CREATED_AT ASC" in sql
    assert "GRANT EXECUTE ON FUNCTION PUBLIC.ORG_ID_FOR_NAME(TEXT) TO CALLPROOF_APP" in sql
    assert "bypass_rls" not in raw
    assert "DEFAULT_ORG_ID" in raw
    assert "0014_org_features_write" in raw


def test_twelfth_revision_short_id_sequence_granted_to_app():
    rev = ROOT / "alembic" / "versions" / "0012_org_members_short_id.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE SEQUENCE ORG_MEMBERS_SHORT_ID_SEQ START 100000" in sql
    assert "ADD COLUMN SHORT_ID INTEGER UNIQUE" in sql
    assert "GRANT USAGE, SELECT ON SEQUENCE ORG_MEMBERS_SHORT_ID_SEQ TO CALLPROOF_APP" in sql
    assert "WHERE SHORT_ID IS NULL" in sql
    assert "OM.SHORT_ID" in sql
    assert "0011_org_members_names" in raw
    assert "GRANT SELECT ON ORG_DIRECTORY TO CALLPROOF_APP" not in sql

