"""password_reset_events: append-only audit trail (AC-16).

Revision ID: 0018_password_reset_events
Revises: 0017_call_audit_attribution
Create Date: 2026-09-02

Investigated first (per the ticket): Supabase's auth.audit_log_entries table
exists but is empty for this project (0 rows despite real activity), and
Supabase Auth Hooks have no "password changed" event to attach to (only
Before User Created / Custom Access Token / Send SMS / Send Email / MFA
Verification Attempt / Password Verification Attempt exist, the last being
about sign-in, not password changes). So this captures the event ourselves,
at the two places our own backend actually knows it happened: the admin
reset-email/direct-reset actions (already ours), and a new self-report call
the frontend makes right after Supabase confirms a self-service change.

Append-only like org_features_history (0016) — INSERT + SELECT only, no
UPDATE/DELETE grant or policy, so callproof_app cannot rewrite a row.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0018_password_reset_events"
down_revision: Union[str, Sequence[str], None] = "0017_call_audit_attribution"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE password_reset_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES org_members (user_id),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('self_service', 'admin_reset_email', 'admin_direct_reset')
            ),
            actor_email TEXT,
            ip_address TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_password_reset_events_org_user
            ON password_reset_events (org_id, user_id, created_at DESC);

        COMMENT ON TABLE password_reset_events IS
            'Append-only password change/reset history. actor_email is the '
            'admin for admin_* event types, null for self_service (the '
            'affected user is their own actor). ip_address is always the '
            'actor''s request IP, not necessarily the affected user''s.';

        GRANT SELECT, INSERT ON password_reset_events TO callproof_app;

        ALTER TABLE password_reset_events ENABLE ROW LEVEL SECURITY;
        CREATE POLICY password_reset_events_select ON password_reset_events
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY password_reset_events_insert ON password_reset_events
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS password_reset_events_insert ON password_reset_events;
        DROP POLICY IF EXISTS password_reset_events_select ON password_reset_events;
        DROP TABLE IF EXISTS password_reset_events;
        """
    )
