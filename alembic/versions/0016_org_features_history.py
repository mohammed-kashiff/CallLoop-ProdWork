"""org_features_history append-only audit trail (AC-1).

Revision ID: 0016_org_features_history
Revises: 0015_org_id_for_name
Create Date: 2026-09-02

Current-state upserts in org_features overwrite the previous value. This table
records every set_feature() call. INSERT + SELECT only — no UPDATE/DELETE
GRANT or policy, so callproof_app cannot rewrite or erase a row.

RLS matches org_features (GUC via callproof_current_org_id), not the
ungranted org_directory pattern. Writes still go through apply_tenant_gucs
on the target org; never a bypass connection.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0016_org_features_history"
down_revision: Union[str, Sequence[str], None] = "0015_org_id_for_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE org_features_history (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
            feature_key TEXT NOT NULL,
            enabled BOOLEAN NOT NULL,
            changed_by TEXT NOT NULL,
            changed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX org_features_history_org_key_at
            ON org_features_history (org_id, feature_key, changed_at);

        COMMENT ON TABLE org_features_history IS
            'Append-only feature-flag changes. One row per set_feature() call. '
            'RLS is org-scoped like org_features, not admin-only like org_directory.';

        GRANT SELECT, INSERT ON org_features_history TO callproof_app;

        ALTER TABLE org_features_history ENABLE ROW LEVEL SECURITY;
        CREATE POLICY org_features_history_select ON org_features_history
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY org_features_history_insert ON org_features_history
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS org_features_history_insert ON org_features_history;
        DROP POLICY IF EXISTS org_features_history_select ON org_features_history;
        DROP TABLE IF EXISTS org_features_history;
        """
    )
