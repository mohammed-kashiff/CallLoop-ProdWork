"""ticket_audits: persist a ticket score so it cannot silently re-score (TA-11).

Revision ID: 0024_ticket_audits
Revises: 0023_ticket_image_assets
Create Date: 2026-09-05

PRD §9: same rescoring-guard principle as calls. An already-audited
ticket must not silently re-run Claude — scores would drift because
the model is not perfectly deterministic. Persistence is what makes
that guard enforceable; TA-10's POST /score had nowhere to look.

One row per ticket (UNIQUE ticket_id). A later allowed re-score
(enable_ticket_rescoring on) updates that row rather than appending.
Not an extension of audits (that table is call_id BIGINT). RLS in
this revision. SELECT/INSERT/UPDATE — upsert on a permitted re-score
needs UPDATE; no DELETE policy.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0024_ticket_audits"
down_revision: Union[str, Sequence[str], None] = "0023_ticket_image_assets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_audits (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            ticket_id UUID NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
            score DOUBLE PRECISION,
            findings JSONB NOT NULL,
            requested_by UUID REFERENCES org_members (user_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (ticket_id)
        );

        CREATE INDEX idx_ticket_audits_org_id ON ticket_audits (org_id);
        CREATE INDEX idx_ticket_audits_ticket_id ON ticket_audits (ticket_id);

        COMMENT ON TABLE ticket_audits IS
            'TA-11. One stored scorecard per ticket. The rescoring guard '
            'reads this row: if it exists, Claude does not run again unless '
            'enable_ticket_rescoring is on for the org. Not the calls audits table.';

        GRANT SELECT, INSERT, UPDATE ON ticket_audits TO callproof_app;

        ALTER TABLE ticket_audits ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_audits_select ON ticket_audits
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_audits_insert ON ticket_audits
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_audits_update ON ticket_audits
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_audits_update ON ticket_audits;
        DROP POLICY IF EXISTS ticket_audits_insert ON ticket_audits;
        DROP POLICY IF EXISTS ticket_audits_select ON ticket_audits;
        DROP TABLE IF EXISTS ticket_audits;
        """
    )
