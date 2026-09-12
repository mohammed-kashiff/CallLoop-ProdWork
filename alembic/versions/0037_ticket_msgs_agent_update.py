"""Grant + RLS policy for UPDATE on ticket_messages.agent_user_id.

Revision ID: 0037_ticket_msgs_agent_update
Revises: 0036_ticket_speaker_display_name
Create Date: 2026-09-12

Found live: the alias-backfill fix (set_alias() in ticket_agent_aliases.py
and ticket_agent_identity_aliases.py) silently updated zero rows in
production despite matching data existing — reproduced directly against
real Postgres: the identical UPDATE returns rowcount=1 with
bypass_rls=True and rowcount=0 through the normal, RLS-respecting
connection callproof_app runs as.

Root cause, confirmed via 0022_tickets.py: ticket_messages was designed
as insert-once, immutable-after-creation — GRANT SELECT, INSERT only,
and only ticket_messages_select/ticket_messages_insert policies. No
UPDATE policy exists, so Postgres RLS silently denies every row for
that command (not an error — a matched-zero-rows no-op), and even with
a policy the role was never granted UPDATE privilege at all. The
backfill feature is the first thing in this codebase that ever needed
to update an existing ticket_messages row.

Scoped as narrowly as Postgres allows: a column-level GRANT restricted
to agent_user_id only, not a blanket UPDATE grant — this table is audit
data, not config, so the app role should never be able to alter the
actual transcript content (text, speaker, sent_at, is_internal, ...)
via any code path, today or in the future. The RLS policy itself still
needs table-level USING/WITH CHECK; the column grant is what keeps the
capability narrow.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0037_ticket_msgs_agent_update"
down_revision: Union[str, Sequence[str], None] = "0036_ticket_speaker_display_name"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        GRANT UPDATE (agent_user_id) ON ticket_messages TO callproof_app;

        CREATE POLICY ticket_messages_update ON ticket_messages
            FOR UPDATE
            USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_messages_update ON ticket_messages;
        REVOKE UPDATE (agent_user_id) ON ticket_messages FROM callproof_app;
        """
    )
