"""ticket_agent_identity_aliases (IN-10).

Revision ID: 0034_ticket_agent_id_alias
Revises: 0033_ticket_messages_internal
Create Date: 2026-09-09

IN-10: the PRD framed keying agent resolution on Intercom's structured
`author.email` as a drop-in swap of TA-15's existing ticket_agent_aliases
mechanism. It isn't one — that table is hard-keyed on (org_id,
display_name), a real unique constraint, and every resolver takes
display_name as its lookup parameter, not an arbitrary identifier.

Rather than retrofit that shipped table (display_name would need to
become nullable, plus a new CHECK that at least one identifier kind is
set, mixing a freeform PDF name and a structured email in one row
shape), this is a new, parallel table keyed generically on
(org_id, provider, identifier) -> user_id. Handles Intercom's email
today and any future provider's own identifier type later, without
ever touching ticket_agent_aliases' contract. Same RLS/grant shape as
that table (0025) - config, not an audit trail, freely editable.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0034_ticket_agent_id_alias"
down_revision: Union[str, Sequence[str], None] = "0033_ticket_messages_internal"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_agent_identity_aliases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            provider TEXT NOT NULL,
            identifier TEXT NOT NULL,
            user_id UUID NOT NULL REFERENCES org_members (user_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (org_id, provider, identifier)
        );

        CREATE INDEX idx_ticket_agent_identity_aliases_org_id
            ON ticket_agent_identity_aliases (org_id);

        COMMENT ON TABLE ticket_agent_identity_aliases IS
            'IN-10. Org owner-managed mapping from a provider-side structured '
            'identifier (e.g. Intercom author.email) to a real '
            'org_members.user_id, resolved at ingestion time. Parallel to '
            'ticket_agent_aliases (TA-15, freeform PDF display names) - a '
            'different identifier kind, kept separate rather than bolted on.';

        GRANT SELECT, INSERT, UPDATE, DELETE ON ticket_agent_identity_aliases TO callproof_app;

        ALTER TABLE ticket_agent_identity_aliases ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_agent_identity_aliases_select ON ticket_agent_identity_aliases
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_identity_aliases_insert ON ticket_agent_identity_aliases
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_identity_aliases_update ON ticket_agent_identity_aliases
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_agent_identity_aliases_delete ON ticket_agent_identity_aliases
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_agent_identity_aliases_delete ON ticket_agent_identity_aliases;
        DROP POLICY IF EXISTS ticket_agent_identity_aliases_update ON ticket_agent_identity_aliases;
        DROP POLICY IF EXISTS ticket_agent_identity_aliases_insert ON ticket_agent_identity_aliases;
        DROP POLICY IF EXISTS ticket_agent_identity_aliases_select ON ticket_agent_identity_aliases;
        DROP TABLE IF EXISTS ticket_agent_identity_aliases;
        """
    )
