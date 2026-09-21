"""training_assignments: persisted coaching drills (assign + complete).

Revision ID: 0047_training_assignments
Revises: 0046_ticket_audits_triggered_by
Create Date: 2026-09-21

GET /api/training still derives suggested drills from Top Gap findings.
Assigning copies a snapshot into this table so Done/reply have a stable
row. Partial unique indexes are per channel — call_id is INTEGER and
ticket_id is UUID, so COALESCE would not type-check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0047_training_assignments"
down_revision: Union[str, Sequence[str], None] = "0046_ticket_audits_triggered_by"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE training_assignments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            assignee_user_id UUID NOT NULL REFERENCES org_members (user_id) ON DELETE CASCADE,
            assigned_by UUID NOT NULL REFERENCES org_members (user_id) ON DELETE RESTRICT,
            channel TEXT NOT NULL CHECK (channel IN ('call', 'ticket')),
            call_id BIGINT REFERENCES calls (id) ON DELETE CASCADE,
            ticket_id UUID REFERENCES tickets (id) ON DELETE CASCADE,
            dimension_id TEXT NOT NULL,
            dimension_name TEXT NOT NULL,
            prompt TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
            reply TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ,
            CHECK (char_length(dimension_id) BETWEEN 1 AND 80),
            CHECK (
                (status = 'done') OR (reply IS NULL)
            ),
            CHECK (
                (channel = 'call' AND call_id IS NOT NULL AND ticket_id IS NULL)
                OR (channel = 'ticket' AND ticket_id IS NOT NULL AND call_id IS NULL)
            )
        );

        CREATE UNIQUE INDEX uq_training_assignments_open_call
            ON training_assignments (org_id, assignee_user_id, call_id, dimension_id)
            WHERE status = 'open' AND channel = 'call' AND call_id IS NOT NULL;

        CREATE UNIQUE INDEX uq_training_assignments_open_ticket
            ON training_assignments (org_id, assignee_user_id, ticket_id, dimension_id)
            WHERE status = 'open' AND channel = 'ticket' AND ticket_id IS NOT NULL;

        CREATE INDEX idx_training_assignments_org_assignee
            ON training_assignments (org_id, assignee_user_id, status, created_at DESC);

        COMMENT ON TABLE training_assignments IS
            'Persisted coaching drills: a manager assigns a Top Gap snapshot; '
            'the assignee marks it done with an optional short reply.';

        GRANT SELECT, INSERT, UPDATE ON training_assignments TO callproof_app;
        REVOKE DELETE ON training_assignments FROM callproof_app;

        ALTER TABLE training_assignments ENABLE ROW LEVEL SECURITY;
        CREATE POLICY training_assignments_select ON training_assignments
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY training_assignments_insert ON training_assignments
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY training_assignments_update ON training_assignments
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS training_assignments_update ON training_assignments;
        DROP POLICY IF EXISTS training_assignments_insert ON training_assignments;
        DROP POLICY IF EXISTS training_assignments_select ON training_assignments;
        DROP TABLE IF EXISTS training_assignments;
        """
    )
