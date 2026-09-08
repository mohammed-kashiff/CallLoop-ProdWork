"""Per-org Vault: names, no plaintext columns, JWT org context."""

from __future__ import annotations

import pytest

from backend.org_ids import DEFAULT_ORG_ID, org_scope
from backend.org_vault import (
    credential_status,
    delete_credential,
    delete_justcall,
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
    rev = ROOT / "alembic" / "versions" / "0030_org_credentials_generic_provider.py"
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
