"""ticket_audits.triggered_by: provenance for the Auto Audit feature.

Revision ID: 0046_ticket_audits_triggered_by
Revises: 0045_ticket_pipeline_events
Create Date: 2026-09-20

IN-31 (Auto Audit for Intercom Tickets, IN-30). A scorecard can now be
produced two ways: a person clicking "Score this ticket" (manual, the
only mode that has ever existed), or automatically the moment a ticket
finishes ingesting from Intercom (auto, IN-33). This column is the
provenance the frontend's "Auto-audited" tag (IN-34) reads.

DEFAULT 'manual' backfills every existing row correctly — nothing in
this codebase could have produced an 'auto' row before this feature
exists, so there is no ambiguity to resolve.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0046_ticket_audits_triggered_by"
down_revision: Union[str, Sequence[str], None] = "0045_ticket_pipeline_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_audits
            ADD COLUMN triggered_by TEXT NOT NULL DEFAULT 'manual';
        ALTER TABLE ticket_audits
            ADD CONSTRAINT ticket_audits_triggered_by_check
                CHECK (triggered_by IN ('manual', 'auto'));

        COMMENT ON COLUMN ticket_audits.triggered_by IS
            'IN-31/IN-33: manual (a person clicked Score) or auto '
            '(Intercom ingestion auto-scored it, enable_ticket_auto_audit). '
            'Set at write time by whichever code path called the scorer.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_audits DROP CONSTRAINT ticket_audits_triggered_by_check;
        ALTER TABLE ticket_audits DROP COLUMN triggered_by;
        """
    )
