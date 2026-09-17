"""Flag turns whose text is an AI-generated image description, not real
customer/agent words.

Revision ID: 0044_ticket_img_desc_flag
Revises: 0043_ticket_display_metadata
Create Date: 2026-09-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0044_ticket_img_desc_flag"
down_revision: Union[str, Sequence[str], None] = "0043_ticket_display_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages
            ADD COLUMN is_image_description BOOLEAN NOT NULL DEFAULT FALSE;
        COMMENT ON COLUMN ticket_messages.is_image_description IS
            'True when text is a Claude-vision-generated description of an '
            'attached image (IN-11/TA-5), not something the customer/agent '
            'actually typed. Set at insert time; existing rows default false '
            '(they predate the bug this column fixes and are not '
            'retroactively reclassified).';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages DROP COLUMN IF EXISTS is_image_description;
        """
    )
