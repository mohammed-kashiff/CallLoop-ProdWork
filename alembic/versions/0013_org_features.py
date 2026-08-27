"""org_features + directory first_seen / last_sign_in (AC-4).

Revision ID: 0013_org_features
Revises: 0012_org_members_short_id
Create Date: 2026-08-28

Missing org+key means enabled (no seed rows). SELECT policy only —
callproof_app can read its GUC org via /api/me. No INSERT/UPDATE/DELETE
policies: member-facing writes are denied by RLS. GRANTs include write so
AC-5 can add org-scoped write policies without another GRANT. Do not
use a bypass connection to write this table.

org_directory stays admin SQL only. Recreate + REVOKE so default privileges
cannot leak it to callproof_app. CI has no auth.users — view is skipped.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0013_org_features"
down_revision: Union[str, Sequence[str], None] = "0012_org_members_short_id"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE org_features (
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            feature_key TEXT NOT NULL,
            enabled BOOLEAN NOT NULL DEFAULT true,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (org_id, feature_key)
        );

        COMMENT ON TABLE org_features IS
            'Per-org UI flags. No row for a key means enabled. '
            'RLS SELECT only for callproof_app; writes are admin-gated (AC-5).';

        GRANT SELECT, INSERT, UPDATE, DELETE ON org_features TO callproof_app;

        ALTER TABLE org_features ENABLE ROW LEVEL SECURITY;
        CREATE POLICY org_features_select ON org_features
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('auth.users') IS NOT NULL THEN
            EXECUTE $view$
              CREATE OR REPLACE VIEW org_directory AS
              SELECT om.user_id, u.email, om.first_name, om.last_name, om.role,
                     om.org_id, o.name AS org_name, om.created_at, om.short_id,
                     u.created_at AS first_seen, u.last_sign_in_at
              FROM org_members om
              JOIN auth.users u ON u.id = om.user_id
              JOIN orgs o ON o.id = om.org_id
            $view$;
            COMMENT ON VIEW org_directory IS
              'Admin directory across orgs. Do not GRANT to callproof_app. '
              'Do not expose via an API without per-org filtering.';
            REVOKE ALL ON org_directory FROM PUBLIC;
            REVOKE ALL ON org_directory FROM callproof_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS org_features_select ON org_features;
        DROP TABLE IF EXISTS org_features;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('auth.users') IS NOT NULL THEN
            EXECUTE $view$
              CREATE OR REPLACE VIEW org_directory AS
              SELECT om.user_id, u.email, om.first_name, om.last_name, om.role,
                     om.org_id, o.name AS org_name, om.created_at, om.short_id
              FROM org_members om
              JOIN auth.users u ON u.id = om.user_id
              JOIN orgs o ON o.id = om.org_id
            $view$;
            COMMENT ON VIEW org_directory IS
              'Admin directory across orgs. Do not GRANT to callproof_app. '
              'Do not expose via an API without per-org filtering.';
            REVOKE ALL ON org_directory FROM PUBLIC;
            REVOKE ALL ON org_directory FROM callproof_app;
          END IF;
        END $$;
        """
    )
