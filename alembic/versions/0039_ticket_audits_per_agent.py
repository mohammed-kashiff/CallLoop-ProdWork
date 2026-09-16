"""TA-21/TA-28: ticket_audits becomes one row per (ticket_id, agent_user_id).

Revision ID: 0039_ticket_audits_per_agent
Revises: 0038_org_members_manager_role
Create Date: 2026-09-16

Per-Agent Ticket Audit Rebuild PRD: every agent who touched a ticket
gets their own independent evaluation, not a post-hoc attribution of one
shared whole-ticket verdict. The old UNIQUE(ticket_id) — one scorecard
per ticket — no longer matches what a row means.

Existing rows predate the per-agent model: their `findings` JSONB is a
whole-ticket scorecard with a `primary_owner` guess, not one specific
agent's independent evaluation, and there's no reliable way to backfill
`agent_user_id` for them without inventing an attribution the old model
never actually computed. Given the current data volume (pilot-stage,
no real customer scorecards depend on this), the honest move is to
clear them rather than carry forward data that doesn't fit the new
schema's meaning — every ticket gets rescored fresh under the real
per-agent model on next score.

agent_user_id is NOT NULL: a row only exists for a real, identified
agent. A ticket with zero resolved agent identities simply has zero
audit rows yet (surfaced as "map agent names first" at the API layer),
never a row scored against nobody in particular.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0039_ticket_audits_per_agent"
down_revision: Union[str, Sequence[str], None] = "0038_org_members_manager_role"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM ticket_audits;

        ALTER TABLE ticket_audits
            ADD COLUMN agent_user_id UUID REFERENCES org_members (user_id);
        ALTER TABLE ticket_audits
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

        ALTER TABLE ticket_audits DROP CONSTRAINT ticket_audits_ticket_id_key;
        ALTER TABLE ticket_audits ALTER COLUMN agent_user_id SET NOT NULL;
        ALTER TABLE ticket_audits
            ADD CONSTRAINT ticket_audits_ticket_agent_key UNIQUE (ticket_id, agent_user_id);

        COMMENT ON TABLE ticket_audits IS
            'TA-21/TA-28. One stored scorecard per (ticket_id, agent_user_id) — '
            'every agent who touched a ticket has their own independent row. '
            'The rescoring guard is now per-agent: a row existing for agent X '
            'blocks Claude from re-running on X without enable_ticket_rescoring, '
            'but a newly-resolved agent Y with no row yet still gets a real '
            'first score even if X was already audited.';
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM ticket_audits;

        ALTER TABLE ticket_audits
            DROP CONSTRAINT ticket_audits_ticket_agent_key;
        ALTER TABLE ticket_audits
            ADD CONSTRAINT ticket_audits_ticket_id_key UNIQUE (ticket_id);

        ALTER TABLE ticket_audits DROP COLUMN updated_at;
        ALTER TABLE ticket_audits DROP COLUMN agent_user_id;
        """
    )
