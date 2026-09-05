"""tickets + ticket_messages (TA-3 / Ticket Audit Engine).

Revision ID: 0022_tickets
Revises: 0021_call_pipeline_events
Create Date: 2026-09-05

Standalone ticket-auditing tables, org-scoped from the first revision that
creates them — same convention as calls/segments (0001) and every later
tenant table. RLS is in this file (not 0005) because these tables did not
exist then.

tickets is mutable: status moves after insert (uploaded → processing →
ready/failed), so SELECT/INSERT/UPDATE like calls. No DELETE policy —
v1 does not hard-delete tickets.

ticket_messages is append-only: written at ingest, never rewritten.
SELECT + INSERT only, same GRANT/policy shape as password_reset_events
(0018) / impersonation_log (0020) / call_pipeline_events (0021).

agent_user_id is the schema decision TA-8 (and per-span v2) depends on.
Nullable FK to org_members(user_id), same pattern as calls.uploaded_by /
audits.requested_by / calls.deleted_by (0017/0019). TA-2 against a real
JustCall PDF: speaker role (agent / customer / bot) is recoverable from
the transcript text; a stable org_members.user_id is not (display name
only). Agent turns may therefore store NULL; customer/bot turns must.
CHECK enforces non-null agent_user_id only on speaker = 'agent'.

No change to rubrics — a Ticket QA rubric (TA-13) is another org-scoped
versioned row in the existing table.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0022_tickets"
down_revision: Union[str, Sequence[str], None] = "0021_call_pipeline_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE tickets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            source TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'uploaded',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_tickets_org_id ON tickets (org_id);
        CREATE INDEX idx_tickets_org_created ON tickets (org_id, created_at DESC);

        COMMENT ON TABLE tickets IS
            'Ticket Audit Engine (TA-3). One row per ingested ticket (PDF '
            'upload for MVP). Mutable status; org_id NOT NULL + RLS from this '
            'revision. Not an extension of calls.';

        GRANT SELECT, INSERT, UPDATE ON tickets TO callproof_app;

        ALTER TABLE tickets ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tickets_select ON tickets
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY tickets_insert ON tickets
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY tickets_update ON tickets
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());

        CREATE TABLE ticket_messages (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            seq INTEGER NOT NULL,
            agent_user_id UUID REFERENCES org_members (user_id),
            speaker TEXT NOT NULL,
            text TEXT,
            sent_at TIMESTAMPTZ,
            UNIQUE (ticket_id, seq),
            CHECK (speaker IN ('agent', 'customer', 'bot')),
            CHECK (agent_user_id IS NULL OR speaker = 'agent')
        );

        CREATE INDEX idx_ticket_messages_org_id ON ticket_messages (org_id);
        CREATE INDEX idx_ticket_messages_ticket_id ON ticket_messages (ticket_id);
        CREATE INDEX idx_ticket_messages_agent_user_id
            ON ticket_messages (agent_user_id);

        COMMENT ON TABLE ticket_messages IS
            'Ordered turns for a ticket. Append-only. agent_user_id is set '
            'only for agent-authored turns when identity is known (nullable '
            'FK to org_members.user_id); TA-8 multi-agent attribution reads '
            'this column. TA-2: role is recoverable from the PDF; a '
            'user_id is not, so agent turns may be NULL.';

        GRANT SELECT, INSERT ON ticket_messages TO callproof_app;

        ALTER TABLE ticket_messages ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_messages_select ON ticket_messages
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_messages_insert ON ticket_messages
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_messages_insert ON ticket_messages;
        DROP POLICY IF EXISTS ticket_messages_select ON ticket_messages;
        DROP TABLE IF EXISTS ticket_messages;
        DROP POLICY IF EXISTS tickets_update ON tickets;
        DROP POLICY IF EXISTS tickets_insert ON tickets;
        DROP POLICY IF EXISTS tickets_select ON tickets;
        DROP TABLE IF EXISTS tickets;
        """
    )
