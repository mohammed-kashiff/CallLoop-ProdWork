"""Per-org Vault: names, no plaintext columns, JWT org context."""

from __future__ import annotations

import pytest

from backend.org_ids import DEFAULT_ORG_ID, org_scope
from backend.org_vault import (
    credential_status,
    delete_credential,
    delete_justcall,
    find_org_id_by_external_account,
    load_credential,
    load_justcall,
    put_credential,
    put_justcall,
    secret_name,
)
from backend.paths import ROOT

ORG_B = "00000000-0000-4000-8000-000000000002"


def test_secret_name_is_justcall_slash_org():
    assert secret_name(DEFAULT_ORG_ID) == f"justcall/{DEFAULT_ORG_ID}"
    with pytest.raises(ValueError):
        secret_name("not-a-uuid")


def test_secret_name_accepts_a_different_provider():
    """IN-4: org_vault is now provider-agnostic — Intercom (or anything
    else) gets the same {provider}/{org_id} naming JustCall always had."""
    assert secret_name(DEFAULT_ORG_ID, "intercom") == f"intercom/{DEFAULT_ORG_ID}"


def test_put_credential_rejects_invalid_provider_names():
    for bad in ("just-call", "", "123provider", "a b", "intercom;drop"):
        with pytest.raises(ValueError, match="invalid provider"):
            put_credential(DEFAULT_ORG_ID, bad, {"token": "x"})


def test_provider_name_is_case_normalized_not_rejected():
    """Mixed-case is a normalization, not a format error — "JustCall" and
    "justcall" must resolve to the same vault secret name."""
    assert secret_name(DEFAULT_ORG_ID, "JustCall") == secret_name(DEFAULT_ORG_ID, "justcall")


def test_put_credential_rejects_empty_or_non_dict_data():
    with pytest.raises(ValueError, match="missing credentials"):
        put_credential(DEFAULT_ORG_ID, "intercom", {})
    with pytest.raises(ValueError, match="missing credentials"):
        put_credential(DEFAULT_ORG_ID, "intercom", None)  # type: ignore[arg-type]


def test_generic_credential_functions_refuse_other_bound_org():
    with org_scope(DEFAULT_ORG_ID):
        with pytest.raises(ValueError, match="org_mismatch"):
            put_credential(ORG_B, "intercom", {"access_token": "tok"})
        with pytest.raises(ValueError, match="org_mismatch"):
            load_credential(ORG_B, "intercom")
        with pytest.raises(ValueError, match="org_mismatch"):
            delete_credential(ORG_B, "intercom")
        with pytest.raises(ValueError, match="org_mismatch"):
            credential_status(ORG_B, "intercom")


def test_justcall_wrappers_delegate_to_the_generic_functions():
    """IN-4: put_justcall/load_justcall/etc. must keep working unchanged for
    every existing call site — verified as thin wrappers, not a parallel
    implementation that could drift from the generic path."""
    src = (ROOT / "backend" / "org_vault.py").read_text(encoding="utf-8")
    assert 'PROVIDER = "justcall"' in src
    wrappers_src = src.split("# ── JustCall wrappers")[1]
    assert "put_credential(" in wrappers_src
    assert "load_credential(" in wrappers_src
    assert "delete_credential(" in wrappers_src
    assert "credential_status(" in wrappers_src
    assert "list_org_ids_for_provider(" in wrappers_src


def test_org_credentials_provider_check_is_generic_not_justcall_only():
    """IN-4: the DB-level CHECK from 0009 was JustCall-only — inserting
    provider='intercom' would fail it before the app code even ran. 0030
    must replace it with a format check, not just widen the enum by one."""
    rev = ROOT / "alembic" / "versions" / "0030_org_credentials_provider.py"
    raw = rev.read_text(encoding="utf-8")
    upgrade_body = raw.split("def upgrade")[1].split("def downgrade")[0]
    assert "org_credentials_provider_check" in upgrade_body
    assert "IN ('justcall')" not in upgrade_body
    assert "~ '^[a-z][a-z0-9_]*$'" in upgrade_body
    # the old enum form is expected in downgrade() — that's the rollback path
    downgrade_body = raw.split("def downgrade")[1]
    assert "IN ('justcall')" in downgrade_body


def test_org_credentials_migration_has_no_plaintext_secret_columns():
    rev = ROOT / "alembic" / "versions" / "0009_org_vault_justcall.py"
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "CREATE TABLE ORG_CREDENTIALS" in sql
    assert "KEY_SUFFIX" in sql
    assert "API_KEY" not in sql
    assert "API_SECRET" not in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "vault.create_secret" in (ROOT / "backend" / "org_vault.py").read_text(encoding="utf-8")


def test_org_vault_module_writes_via_vault_functions():
    src = (ROOT / "backend" / "org_vault.py").read_text(encoding="utf-8")
    assert "vault.create_secret" in src
    assert "vault.update_secret" in src
    assert "vault.decrypted_secrets" in src
    assert "INSERT INTO org_credentials" in src
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("log.") or "applog.event" in stripped:
            lowered = stripped.lower()
            assert "api_key" not in lowered
            assert "api_secret" not in lowered
            assert "payload" not in lowered
            assert "decrypted" not in lowered


def test_put_load_delete_refuse_other_bound_org():
    key = "jc_key_abcdefgh"
    secret = "jc_sec_ijklmnop"
    with org_scope(DEFAULT_ORG_ID):
        with pytest.raises(ValueError, match="org_mismatch"):
            put_justcall(ORG_B, key, secret)
        with pytest.raises(ValueError, match="org_mismatch"):
            load_justcall(ORG_B)
        with pytest.raises(ValueError, match="org_mismatch"):
            delete_justcall(ORG_B)


def test_find_org_id_by_external_account_short_circuits_on_empty_id():
    """IN-5: no DB patched here at all — if this hit db.connection, it
    would raise (real connection, no test DB configured for this path).
    Proves the empty-string guard fires before any query."""
    assert find_org_id_by_external_account("intercom", "") is None
    assert find_org_id_by_external_account("intercom", None) is None


def test_find_org_id_by_external_account_validates_provider_before_any_db_call():
    with pytest.raises(ValueError, match="invalid provider"):
        find_org_id_by_external_account("Not Valid!", "app_123")


def test_find_org_id_by_external_account_returns_org_id_via_stub(monkeypatch):
    """Full round-trip needs a real Postgres row; stubbed here at the
    db.connection boundary to check find_org_id_by_external_account's own
    SQL shape and return-value handling in isolation."""
    class _Result:
        def __init__(self, row):
            self._row = row

        def fetchone(self):
            return self._row

    class _FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            norm = " ".join(str(sql).split()).upper()
            assert norm.startswith("SELECT ORG_ID FROM ORG_CREDENTIALS")
            assert params == ("intercom", "app_abc123")
            return _Result({"org_id": DEFAULT_ORG_ID})

    from backend import org_vault as ov

    monkeypatch.setattr(ov.db, "connection", lambda **kw: _FakeConn())
    assert find_org_id_by_external_account("intercom", "app_abc123") == DEFAULT_ORG_ID


def test_put_credential_sql_includes_external_account_id_column():
    """Source-level check that the INSERT actually persists the routing
    column IN-5 depends on, without needing a live Postgres row."""
    src = (ROOT / "backend" / "org_vault.py").read_text(encoding="utf-8")
    put_credential_src = src.split("def put_credential")[1].split("def load_credential")[0]
    assert "external_account_id" in put_credential_src


# ---------- live Postgres: real Vault, the actual bug from IN-15's pilot ----------


def test_put_credential_refuses_a_second_org_for_the_same_workspace(monkeypatch):
    """Real regression: connecting the same real Intercom workspace to a
    second org used to raise a raw UniqueViolation from Postgres (the
    ON CONFLICT clause only covers (org_id, provider), not the separate
    (provider, external_account_id) index) — a 500 straight out of the
    OAuth callback instead of a clean, actionable error. Needs real Vault
    (vault.create_secret), so this only runs against a real Supabase
    project, same convention as test_ticket_ingest.py's live tests."""
    import os
    import uuid

    from dotenv import dotenv_values

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    raw_env = dotenv_values(ENV_FILE)
    real_supabase_url = raw_env.get("SUPABASE_URL")
    real_service_role_key = raw_env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not real_supabase_url or not real_service_role_key or "test.supabase.co" in real_supabase_url:
        pytest.skip("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set")
    monkeypatch.setenv("SUPABASE_URL", real_supabase_url)
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", real_service_role_key)
    for key, value in raw_env.items():
        if key not in os.environ and value is not None:
            monkeypatch.setenv(key, value)

    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import org_vault as ov

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    workspace_id = f"live-test-{uuid.uuid4().hex[:12]}"
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_a, "vault-conflict-a"))
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "vault-conflict-b"))
        admin.commit()

        with org_scope(org_a):
            ov.put_credential(
                org_a, "intercom", {"access_token": "tok-a"},
                external_account_id=workspace_id,
            )
            # Same org reconnecting its own workspace stays a clean upsert.
            ov.put_credential(
                org_a, "intercom", {"access_token": "tok-a-refreshed"},
                external_account_id=workspace_id,
            )

        with org_scope(org_b):
            with pytest.raises(ov.CredentialConflict):
                ov.put_credential(
                    org_b, "intercom", {"access_token": "tok-b"},
                    external_account_id=workspace_id,
                )
    finally:
        with org_scope(org_a):
            ov.delete_credential(org_a, "intercom")
        with org_scope(org_b):
            ov.delete_credential(org_b, "intercom")
        admin.execute("DELETE FROM orgs WHERE id IN (%s, %s)", (org_a, org_b))
        admin.commit()
        admin.close()
