"""ticket_messages.is_internal (IN-9).

Revision ID: 0033_ticket_messages_internal
Revises: 0032_ticket_provider_stats
Create Date: 2026-09-09

IN-9: the original design assumed note-type Intercom parts (part_type
"note" or a "note_and_*" combined-action variant, e.g. "note_and_unsnooze")
were non-substantive and could be discarded. Wrong — a real sample showed a
decisive, customer-impacting answer delivered entirely through a note,
never surfaced as a customer-facing comment. Notes are now captured, not
discarded, and tagged here so scoring can tell them apart: a dimension
that specifically judges customer-facing communication (e.g. Tone)
excludes them; a dimension about internal decision quality or handoff
correctness (e.g. Ownership, Diagnostic Reasoning) can include them.

NOT NULL DEFAULT false — every existing row (PDF-sourced tickets, and the
Intercom-sourced ones ingested before this shipped) is customer-facing by
definition for this column; not retroactively re-classified.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0033_ticket_messages_internal"
down_revision: Union[str, Sequence[str], None] = "0032_ticket_provider_stats"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages
            ADD COLUMN is_internal BOOLEAN NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages DROP COLUMN IF EXISTS is_internal;
        """
    )
