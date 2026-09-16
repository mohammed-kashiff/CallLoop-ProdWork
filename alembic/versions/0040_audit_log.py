"""audit_log: durable, append-only record of who did what (AC-63/AC-65).

Revision ID: 0040_audit_log
Revises: 0039_ticket_audits_per_agent
Create Date: 2026-09-16

Log lines roll off retention and were never meant to be a compliance
record. This is the durable half of the Actor Identity & Audit Trail
epic — one row per real state-changing action (role changed, credential
saved, rubric activated, alias mapped, export generated...), never for
plain reads (those stay as regular actor-tagged log lines, AC-64, so this
table doesn't balloon with noise and the real signal — who changed
something — doesn't get buried).

Same proven shape as impersonation_log (0020)/password_reset_events
(0018): append-only (GRANT SELECT, INSERT only, never UPDATE/DELETE — an
admin must not be able to erase their own trail), org-scoped RLS via
callproof_current_org_id(). actor_id/actor_email are denormalized, not
FK'd to org_members, same reasoning as impersonation_log's admin_email:
a platform admin acting across an org boundary they're not a member of
(creating an org, promoting another admin) is a real, expected case, and
the row must stay meaningful even if the actor is later removed or
renamed.

A platform admin's "see every org's activity" view (AC-69) is a separate,
narrowly-scoped bypass_rls read function on top of this table's normal
RLS — the same pattern org_vault.py's org_directory lookup and
admin_console.py's search_directory already use — not a weaker policy
here.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0040_audit_log"
down_revision: Union[str, Sequence[str], None] = "0039_ticket_audits_per_agent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE audit_log (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            actor_id UUID,
            actor_email TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            before JSONB,
            after JSONB,
            ip_address TEXT,
            request_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_audit_log_org_created
            ON audit_log (org_id, created_at DESC);
        CREATE INDEX idx_audit_log_action
            ON audit_log (action, created_at DESC);
        CREATE INDEX idx_audit_log_actor
            ON audit_log (actor_id, created_at DESC);

        COMMENT ON TABLE audit_log IS
            'AC-63/AC-65. Append-only record of who did what — one row per '
            'real state-changing action, not for plain reads (those stay as '
            'actor-tagged log lines only). actor_id/actor_email denormalized, '
            'not FK''d to org_members, since a platform admin can act across '
            'an org boundary they are not a member of.';

        GRANT SELECT, INSERT ON audit_log TO callproof_app;

        ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;
        CREATE POLICY audit_log_select ON audit_log
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY audit_log_insert ON audit_log
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS audit_log_insert ON audit_log;
        DROP POLICY IF EXISTS audit_log_select ON audit_log;
        DROP TABLE IF EXISTS audit_log;
        """
    )
