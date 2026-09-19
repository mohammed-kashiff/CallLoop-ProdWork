"""ticket_pipeline_events: append-only per-ticket pipeline trail.

Revision ID: 0045_ticket_pipeline_events
Revises: 0044_performance_kpis
Create Date: 2026-09-19

Mirror of call_pipeline_events (0021) for Ticket Audit. Ticket ids are
UUIDs (calls are integers), so this is a sibling table rather than a
nullable call_id column. Stages are recorded by backend/ticket_trail.py:
parse, image_describe, agent_resolve, scoring, criterion:{id},
result_served. Scoring is per-agent; agent_user_id lives in detail JSONB,
not as a column — same freeform-detail convention as 0021.

INSERT + SELECT only, no UPDATE/DELETE grant or policy. RLS is org-scoped
via callproof_current_org_id(). Explicit GRANT SELECT, INSERT so this
table does not inherit blanket default privileges.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0045_ticket_pipeline_events"
down_revision: Union[str, Sequence[str], None] = "0044_performance_kpis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE ticket_pipeline_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            ticket_id UUID NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
            detail JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_ticket_pipeline_events_ticket
            ON ticket_pipeline_events (ticket_id, created_at);
        CREATE INDEX idx_ticket_pipeline_events_org_created
            ON ticket_pipeline_events (org_id, created_at DESC);

        COMMENT ON TABLE ticket_pipeline_events IS
            'Append-only per-ticket pipeline trail: parse, screenshot '
            'describe, agent resolve, per-agent per-criterion scoring, '
            'and every time a score was served. One row per stage '
            'transition, including every failure with its cause.';

        GRANT SELECT, INSERT ON ticket_pipeline_events TO callproof_app;
        REVOKE UPDATE, DELETE ON ticket_pipeline_events FROM callproof_app;

        ALTER TABLE ticket_pipeline_events ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_pipeline_events_select ON ticket_pipeline_events
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_pipeline_events_insert ON ticket_pipeline_events
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_pipeline_events_insert ON ticket_pipeline_events;
        DROP POLICY IF EXISTS ticket_pipeline_events_select ON ticket_pipeline_events;
        DROP TABLE IF EXISTS ticket_pipeline_events;
        """
    )
