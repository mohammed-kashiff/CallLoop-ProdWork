"""Per-user attribution on calls/audits (AC-14).

Revision ID: 0017_call_audit_attribution
Revises: 0016_org_features_history
Create Date: 2026-09-02

calls.uploaded_by / audits.requested_by: who performed the action, not just
which org. Both nullable — existing rows can't be backfilled (no historical
data to recover), and background ingest (JustCall webhook/poller) has no
acting user either. FK to org_members(user_id), which already carries a
UNIQUE constraint (0004_org_members.py), so this is a plain FK, not a
composite one.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0017_call_audit_attribution"
down_revision: Union[str, Sequence[str], None] = "0016_org_features_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE calls
            ADD COLUMN uploaded_by UUID REFERENCES org_members (user_id);

        ALTER TABLE audits
            ADD COLUMN requested_by UUID REFERENCES org_members (user_id);

        CREATE INDEX idx_calls_uploaded_by ON calls (uploaded_by);
        CREATE INDEX idx_audits_requested_by ON audits (requested_by);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_audits_requested_by;
        DROP INDEX IF EXISTS idx_calls_uploaded_by;
        ALTER TABLE audits DROP COLUMN IF EXISTS requested_by;
        ALTER TABLE calls DROP COLUMN IF EXISTS uploaded_by;
        """
    )
