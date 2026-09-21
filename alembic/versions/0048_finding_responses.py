"""0048_finding_responses: agent agree/dispute on a scored criterion.

Revision ID: 0048_finding_responses
Revises: 0047_training_assignments
Create Date: 2026-09-21

One stance per agent per finding. Partial unique indexes are per channel
— call_id is INTEGER and ticket_id is UUID, so COALESCE would not
type-check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0048_finding_responses"
down_revision: Union[str, Sequence[str], None] = "0047_training_assignments"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE finding_responses (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES org_members (user_id) ON DELETE CASCADE,
            channel TEXT NOT NULL CHECK (channel IN ('call', 'ticket')),
            call_id BIGINT REFERENCES calls (id) ON DELETE CASCADE,
            ticket_id UUID REFERENCES tickets (id) ON DELETE CASCADE,
            dimension_id TEXT NOT NULL,
            stance TEXT NOT NULL CHECK (stance IN ('agree', 'dispute')),
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (char_length(dimension_id) BETWEEN 1 AND 80),
            CHECK (
                (stance = 'agree') OR (note IS NOT NULL AND char_length(note) BETWEEN 1 AND 400)
            ),
            CHECK (
                (channel = 'call' AND call_id IS NOT NULL AND ticket_id IS NULL)
                OR (channel = 'ticket' AND ticket_id IS NOT NULL AND call_id IS NULL)
            )
        );

        CREATE UNIQUE INDEX uq_finding_responses_call
            ON finding_responses (org_id, user_id, call_id, dimension_id)
            WHERE channel = 'call' AND call_id IS NOT NULL;

        CREATE UNIQUE INDEX uq_finding_responses_ticket
            ON finding_responses (org_id, user_id, ticket_id, dimension_id)
            WHERE channel = 'ticket' AND ticket_id IS NOT NULL;

        CREATE INDEX idx_finding_responses_org_stance
            ON finding_responses (org_id, stance, updated_at DESC);

        COMMENT ON TABLE finding_responses IS
            'Scored agent agree/dispute on one criterion. Note required on dispute.';

        GRANT SELECT, INSERT, UPDATE ON finding_responses TO callproof_app;
        REVOKE DELETE ON finding_responses FROM callproof_app;

        ALTER TABLE finding_responses ENABLE ROW LEVEL SECURITY;
        CREATE POLICY finding_responses_select ON finding_responses
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY finding_responses_insert ON finding_responses
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY finding_responses_update ON finding_responses
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS finding_responses_update ON finding_responses;
        DROP POLICY IF EXISTS finding_responses_insert ON finding_responses;
        DROP POLICY IF EXISTS finding_responses_select ON finding_responses;
        DROP TABLE IF EXISTS finding_responses;
        """
    )
