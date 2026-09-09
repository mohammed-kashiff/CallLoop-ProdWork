"""tickets.provider_extra (IN-13).

Revision ID: 0035_ticket_provider_extra
Revises: 0034_ticket_agent_id_alias
Create Date: 2026-09-09

IN-13: Intercom returns several other pre-computed fields on a
Conversation beyond `statistics` (IN-7/0032) - captured here, raw and
unused, "if cheap to do so" per the story's own v1 discipline: no UI,
no scoring, until there's a specific reason to build either.

Corrected while building: the story names one of these "sentiment" -
Intercom has no such field. Checked its real OpenAPI spec directly
(same discipline as IN-6/IN-7/IN-8's own corrections) - the real,
existing field is `conversation_rating` (a 1-5 CSAT-style rating).
Stored here instead, alongside the other three real fields:
`custom_attributes`, `sla_applied`, `ai_agent`. All four exist only on
the Conversation object, not Ticket - same asymmetry `statistics`
already has - so this is wired into the conversation ingest path only.

Separate column from provider_stats (0032) rather than folding these
in - provider_stats already has an established, documented meaning
("Intercom's statistics field, v1"); redefining it to also mean "and
some other stuff" would blur that rather than extend it cleanly.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0035_ticket_provider_extra"
down_revision: Union[str, Sequence[str], None] = "0034_ticket_agent_id_alias"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets ADD COLUMN provider_extra JSONB;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets DROP COLUMN IF EXISTS provider_extra;
        """
    )
