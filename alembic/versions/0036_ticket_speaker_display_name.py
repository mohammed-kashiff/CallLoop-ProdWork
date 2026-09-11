"""Generalize ticket_messages.agent_display_name to speaker_display_name.

Revision ID: 0036_ticket_speaker_display_name
Revises: 0035_ticket_provider_extra
Create Date: 2026-09-12

Found live during IN-15's pilot: a customer turn's raw name/email is
computed at parse time on both ingestion paths (ticket_pdf_parser.py's
_speaker_display_name(), intercom_ingest.py's _speaker_name()) but was
discarded before the write — ticket_ingest.insert_ticket_messages() only
kept it for speaker == "agent" (TA-15's original, deliberately agent-only
scope), so the ticket UI could only ever show the generic role label
"Customer" for a customer turn, never a name.

This renames the column so it carries a raw display name for *any*
speaker, not just agents. Existing agent rows keep their values —
same column, same data, just no longer artificially NULL for the other
two roles going forward. ticket_agent_aliases.py's unresolved-agent
listing already filters on agent_user_id being unset; it gets an
explicit speaker = 'agent' guard in the same commit so it doesn't start
surfacing customer names as "agents to alias" now that the column is no
longer implicitly agent-only via NULL.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0036_ticket_speaker_display_name"
down_revision: Union[str, Sequence[str], None] = "0035_ticket_provider_extra"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages
            RENAME COLUMN agent_display_name TO speaker_display_name;

        COMMENT ON COLUMN ticket_messages.speaker_display_name IS
            'Raw display name/identifier off the source (PDF name or '
            'Intercom email) for any speaker role, regardless of whether '
            'agent_user_id resolved. Was agent-only (agent_display_name, '
            'TA-15) until customer turns needed the same treatment.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages
            RENAME COLUMN speaker_display_name TO agent_display_name;

        COMMENT ON COLUMN ticket_messages.agent_display_name IS
            'TA-15. Raw display name off the PDF for an agent turn (e.g. '
            '"Kashif"), regardless of whether agent_user_id resolved. NULL '
            'for customer/bot turns.';
        """
    )
