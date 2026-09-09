"""tickets.provider_stats (raw Intercom statistics, v1).

Revision ID: 0032_ticket_provider_stats
Revises: 0031_ticket_intercom_dedup
Create Date: 2026-09-09

IN-7: Intercom conversations and tickets both carry a `statistics` object
(first-response time, resolution time, etc.). v1 decision: store it as-is,
unparsed — no typed columns, not surfaced in the UI yet — since there's no
confirmed UI need for it beyond having it captured. NULL for PDF-sourced
tickets, which have no such concept.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0032_ticket_provider_stats"
down_revision: Union[str, Sequence[str], None] = "0031_ticket_intercom_dedup"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets ADD COLUMN provider_stats JSONB;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets DROP COLUMN IF EXISTS provider_stats;
        """
    )
