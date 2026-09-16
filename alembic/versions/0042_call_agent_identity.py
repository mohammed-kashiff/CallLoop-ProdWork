"""calls.agent_user_id/agent_identifier + call_agent_identity_aliases (IN-22/23).

Revision ID: 0042_call_agent_identity
Revises: 0041_api_usage_actor
Create Date: 2026-09-17

Prerequisite for the Team Performance Dashboard PRD: calls had no real
per-agent identity the way tickets now do (TA-15/IN-10) — only
`uploaded_by` (who performed the upload action) and a heuristic name
guessed from the call's own opening transcript lines
(`api._agent_display_name`). Neither is a resolved FK.

`agent_user_id` is the resolved identity a dashboard/report reads.
`agent_identifier` is the raw signal a later alias mapping backfills
against — same role `ticket_messages.speaker_display_name` plays for
PDF tickets. For a JustCall-synced call this is the real `agent_email`
already present in JustCall's own webhook/API payload (previously only
used to build a cosmetic display label, never captured for identity
resolution); for a manually uploaded call there is nothing to store
here since `uploaded_by` already is the resolved identity directly.

Backfill: `agent_user_id = uploaded_by` for every existing row where
`uploaded_by IS NOT NULL` — the manual-upload case was already
resolvable, no reason to leave it NULL until a report runs. JustCall-
synced rows with no `uploaded_by` stay NULL until an owner maps an
alias (IN-24's set_alias() backfills those retroactively, same
transaction, same fix TA-15/IN-10 needed found live — applied here on
day one instead).

call_agent_identity_aliases is a structural copy of
ticket_agent_identity_aliases (0034/IN-10): generic on
(org_id, provider, identifier) -> user_id, not retrofit onto any
call-specific table, so a future provider's own identifier type slots
in without a new table. Same RLS/grant shape — config, not an audit
trail, freely editable.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0042_call_agent_identity"
down_revision: Union[str, Sequence[str], None] = "0041_api_usage_actor"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE calls ADD COLUMN agent_user_id UUID REFERENCES org_members (user_id);
        ALTER TABLE calls ADD COLUMN agent_identifier TEXT;

        CREATE INDEX idx_calls_agent_user_id ON calls (agent_user_id);

        UPDATE calls SET agent_user_id = uploaded_by WHERE uploaded_by IS NOT NULL;

        COMMENT ON COLUMN calls.agent_user_id IS
            'IN-22/23. Resolved real identity of the agent on this call — '
            'defaults to uploaded_by for a manual upload, resolved via '
            'call_agent_identity_aliases for a JustCall-synced call, or set '
            'directly by an owner/manager reassignment (IN-26). NULL means '
            'genuinely unresolved, never a guess.';
        COMMENT ON COLUMN calls.agent_identifier IS
            'IN-22/23. Raw provider-side identifier (JustCall agent_email) '
            'for a still-unresolved call — what set_alias() backfills '
            'against once an owner maps it. NULL for manually uploaded '
            'calls, which resolve directly via uploaded_by instead.';

        CREATE TABLE call_agent_identity_aliases (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            provider TEXT NOT NULL,
            identifier TEXT NOT NULL,
            user_id UUID NOT NULL REFERENCES org_members (user_id),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (org_id, provider, identifier)
        );

        CREATE INDEX idx_call_agent_identity_aliases_org_id
            ON call_agent_identity_aliases (org_id);

        COMMENT ON TABLE call_agent_identity_aliases IS
            'IN-22/23. Org owner-managed mapping from a provider-side '
            'structured identifier (e.g. JustCall agent_email) to a real '
            'org_members.user_id, resolved at ingestion time. Parallel to '
            'ticket_agent_identity_aliases (IN-10) — same shape, calls '
            'instead of tickets, kept separate rather than bolted on.';

        GRANT SELECT, INSERT, UPDATE, DELETE ON call_agent_identity_aliases TO callproof_app;

        ALTER TABLE call_agent_identity_aliases ENABLE ROW LEVEL SECURITY;
        CREATE POLICY call_agent_identity_aliases_select ON call_agent_identity_aliases
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY call_agent_identity_aliases_insert ON call_agent_identity_aliases
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY call_agent_identity_aliases_update ON call_agent_identity_aliases
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY call_agent_identity_aliases_delete ON call_agent_identity_aliases
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS call_agent_identity_aliases_delete ON call_agent_identity_aliases;
        DROP POLICY IF EXISTS call_agent_identity_aliases_update ON call_agent_identity_aliases;
        DROP POLICY IF EXISTS call_agent_identity_aliases_insert ON call_agent_identity_aliases;
        DROP POLICY IF EXISTS call_agent_identity_aliases_select ON call_agent_identity_aliases;
        DROP TABLE IF EXISTS call_agent_identity_aliases;

        ALTER TABLE calls DROP COLUMN IF EXISTS agent_identifier;
        ALTER TABLE calls DROP COLUMN IF EXISTS agent_user_id;
        """
    )
