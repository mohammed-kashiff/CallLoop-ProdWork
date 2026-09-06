"""ticket_agent_aliases + ticket_messages.agent_display_name (TA-15).

Revision ID: 0025_ticket_agent_aliases
Revises: 0024_ticket_audits
Create Date: 2026-09-06

TA-15: the PDF parser can identify an agent turn's raw display name
("Kashif", "Tanu") but has no way to resolve it to a real
org_members.user_id — org_members stores no display name to match
against. Two changes:

1. ticket_messages.agent_display_name — the raw name, nullable,
   populated at ingestion regardless of whether it resolves. Previously
   this was parsed and then discarded (ticket_pdf_parser.py's own
   docstring flagged this as a known gap). Backward compatible: existing
   rows get NULL, nothing reads or writes this column outside the new
   ingestion/alias code.

2. ticket_agent_aliases — an org owner's own mapping from a raw display
   name to a real org_members.user_id, resolved automatically at
   ingestion time for any future ticket carrying that name. Config, not
   an audit trail (unlike impersonation_log/call_pipeline_events) — an
   owner can change or remove a mapping, so SELECT/INSERT/UPDATE/DELETE
   all apply, same shape as org_features. UNIQUE (org_id, display_name):
   one person per name per org.

Does not retroactively fix agent_user_id on tickets ingested before a
mapping existed — only new ingestions benefit. A known, accepted v1
boundary (see TA-15's own ticket), not addressed by this revision.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0025_ticket_agent_aliases"
down_revision: Union[str, Sequence[str], None] = "0024_ticket_audits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE ticket_messages ADD COLUMN agent_display_name TEXT;

        CREATE TABLE ticket_agent_aliases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            display_name TEXT NOT NULL,
            user_id UUID NOT NULL REFERENCES org_members (user_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (org_id, display_name)
        );

        CREATE INDEX idx_ticket_agent_aliases_org_id ON ticket_agent_aliases (org_id);

        COMMENT ON TABLE ticket_agent_aliases IS
            'TA-15. Org owner-managed mapping from a ticket PDF''s raw agent '
            'display name to a real org_members.user_id, resolved at ingestion '
            'time. Config, not an audit trail — freely editable.';
        COMMENT ON COLUMN ticket_messages.agent_display_name IS
            'TA-15. Raw display name off the PDF for an agent turn (e.g. '
            '"Kashif"), regardless of whether agent_user_id resolved. NULL '
            'for customer/bot turns.';

        GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_agent_aliases TO callproof_app;

        ALTER TABLE ticket_agent_aliases ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_agent_aliases_select ON ticket_agent_aliases
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_aliases_insert ON ticket_agent_aliases
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_aliases_update ON ticket_agent_aliases
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_aliases_delete ON ticket_agent_aliases
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_agent_aliases_delete ON ticket_agent_aliases;
        DROP POLICY IF EXISTS ticket_agent_aliases_update ON ticket_agent_aliases;
        DROP POLICY IF EXISTS ticket_agent_aliases_insert ON ticket_agent_aliases;
        DROP POLICY IF EXISTS ticket_agent_aliases_select ON ticket_agent_aliases;
        DROP TABLE IF EXISTS ticket_agent_aliases;
        ALTER TABLE ticket_messages DROP COLUMN IF EXISTS agent_display_name;
        """
    )
