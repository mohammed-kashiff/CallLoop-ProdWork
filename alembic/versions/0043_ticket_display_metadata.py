"""Ticket display metadata for conversation-parity headers.

Revision ID: 0043_ticket_display_metadata
Revises: 0042_call_agent_identity
Create Date: 2026-09-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0043_ticket_display_metadata"
down_revision: Union[str, Sequence[str], None] = "0042_call_agent_identity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets
            ADD COLUMN subject TEXT,
            ADD COLUMN provider_status TEXT,
            ADD COLUMN provider_created_at TIMESTAMPTZ,
            ADD COLUMN closed_at TIMESTAMPTZ,
            ADD COLUMN tags JSONB;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets
            DROP COLUMN IF EXISTS tags,
            DROP COLUMN IF EXISTS closed_at,
            DROP COLUMN IF EXISTS provider_created_at,
            DROP COLUMN IF EXISTS provider_status,
            DROP COLUMN IF EXISTS subject;
        """
    )
