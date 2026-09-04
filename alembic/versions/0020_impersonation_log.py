"""impersonation_log: append-only audit trail for admin "log in as" (CC-1).

Revision ID: 0020_impersonation_log
Revises: 0019_call_soft_delete
Create Date: 2026-09-04

Admin-only, no live consent step (explicit product decision): any platform
admin can mint a session for any org member at any time. This table is the
accountability mechanism, not a gate — never customer-facing, but permanent
so a rogue/compromised admin credential or a customer's "who accessed my
account" request both have a real answer.

Same shape as password_reset_events (0018): append-only, org-scoped RLS via
callproof_current_org_id(), written through apply_tenant_gucs on the target
org — never a bypass connection, so a future customer-facing surface could
read a slice of this safely without a new grant.

target_email is denormalized (fetched at impersonation time) rather than
always joined from org_directory, same reasoning as actor_email on
password_reset_events: the row must stay meaningful even if the member is
later removed or renamed.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0020_impersonation_log"
down_revision: Union[str, Sequence[str], None] = "0019_call_soft_delete"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE impersonation_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            admin_email TEXT NOT NULL,
            target_user_id UUID NOT NULL REFERENCES org_members (user_id),
            target_email TEXT NOT NULL,
            ip_address TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_impersonation_log_org_created
            ON impersonation_log (org_id, created_at DESC);
        CREATE INDEX idx_impersonation_log_target_user
            ON impersonation_log (target_user_id, created_at DESC);

        COMMENT ON TABLE impersonation_log IS
            'Append-only record of every admin "log in as" session mint. '
            'admin_email is the platform admin who acted; target_user_id/'
            'target_email is whose session was minted. Never exposed to a '
            'customer-facing route today; RLS is org-scoped so it could be '
            'later without a new grant.';

        GRANT SELECT, INSERT ON impersonation_log TO callproof_app;

        ALTER TABLE impersonation_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY impersonation_log_select ON impersonation_log
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY impersonation_log_insert ON impersonation_log
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS impersonation_log_insert ON impersonation_log;
        DROP POLICY IF EXISTS impersonation_log_select ON impersonation_log;
        DROP TABLE IF EXISTS impersonation_log;
        """
    )
