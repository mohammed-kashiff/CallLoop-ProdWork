"""Per-org Vault: names, no plaintext columns, JWT org context."""

from __future__ import annotations

import pytest

from backend.org_ids import DEFAULT_ORG_ID, org_scope
from backend.org_vault import delete_justcall, load_justcall, put_justcall, secret_name
from backend.paths import ROOT

ORG_B = "00000000-0000-4000-8000-000000000002"


def test_secret_name_is_justcall_slash_org():
    assert secret_name(DEFAULT_ORG_ID) == f"justcall/{DEFAULT_ORG_ID}"
    with pytest.raises(ValueError):
        secret_name("not-a-uuid")


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
