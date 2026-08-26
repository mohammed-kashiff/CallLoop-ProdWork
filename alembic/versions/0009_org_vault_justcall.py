"""Per-org JustCall credentials in Supabase Vault (no plaintext secret columns).

Revision ID: 0009_org_vault_justcall
Revises: 0008_storage_audio_bucket
Create Date: 2026-08-27

org_credentials holds org_id + provider + key_suffix only.
The API key/secret are written with vault.create_secret / update_secret.
CI Postgres has no vault schema — grants are skipped; runtime put() then 503s.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0009_org_vault_justcall"
down_revision: Union[str, Sequence[str], None] = "0008_storage_audio_bucket"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE org_credentials (
            org_id uuid NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            provider text NOT NULL,
            key_suffix text,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, provider),
            CONSTRAINT org_credentials_provider_check
                CHECK (provider IN ('justcall'))
        );

        COMMENT ON TABLE org_credentials IS
            'Per-org integration index. Secrets live in vault.secrets, not here.';

        GRANT SELECT, INSERT, UPDATE, DELETE ON org_credentials TO callproof_app;

        ALTER TABLE org_credentials ENABLE ROW LEVEL SECURITY;
        CREATE POLICY org_credentials_select ON org_credentials
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY org_credentials_insert ON org_credentials
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY org_credentials_update ON org_credentials
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY org_credentials_delete ON org_credentials
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'vault') THEN
            GRANT USAGE ON SCHEMA vault TO callproof_app;
            IF to_regclass('vault.secrets') IS NOT NULL THEN
              GRANT SELECT, DELETE ON vault.secrets TO callproof_app;
            END IF;
            IF to_regclass('vault.decrypted_secrets') IS NOT NULL THEN
              GRANT SELECT ON vault.decrypted_secrets TO callproof_app;
            END IF;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS org_credentials_select ON org_credentials;
        DROP POLICY IF EXISTS org_credentials_insert ON org_credentials;
        DROP POLICY IF EXISTS org_credentials_update ON org_credentials;
        DROP POLICY IF EXISTS org_credentials_delete ON org_credentials;
        DROP TABLE IF EXISTS org_credentials;
        """
    )
