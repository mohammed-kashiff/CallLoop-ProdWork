"""performance_kpis: org-default and per-agent criterion targets.

Revision ID: 0044_performance_kpis
Revises: 0043_ticket_display_metadata
Create Date: 2026-09-18

Assignable KPIs for Team Performance. A row with agent_user_id NULL is
the org default for that channel + dimension; a UUID is an override for
one teammate. dimension_id is a live rubric criterion id, or
__overall__ for ticket/call average score. Targets are 0–100.

GET merges the active rubric's dimension list with these rows — a new
custom criterion does not insert a row until someone sets a target.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0044_performance_kpis"
down_revision: Union[str, Sequence[str], None] = "0043_ticket_display_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE performance_kpis (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            channel TEXT NOT NULL CHECK (channel IN ('ticket', 'call')),
            dimension_id TEXT NOT NULL,
            agent_user_id UUID REFERENCES org_members (user_id) ON DELETE CASCADE,
            target NUMERIC(5, 2) NOT NULL CHECK (target >= 0 AND target <= 100),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CHECK (char_length(dimension_id) BETWEEN 1 AND 80)
        );

        CREATE UNIQUE INDEX performance_kpis_scope
            ON performance_kpis (
                org_id,
                channel,
                dimension_id,
                COALESCE(agent_user_id, '00000000-0000-0000-0000-000000000000')
            );

        CREATE INDEX idx_performance_kpis_org_id ON performance_kpis (org_id);

        COMMENT ON TABLE performance_kpis IS
            'Org-default (agent_user_id NULL) and per-agent KPI targets '
            'for a rubric dimension or __overall__. Config, not an audit trail.';

        GRANT SELECT, INSERT, UPDATE, DELETE ON performance_kpis TO callproof_app;

        ALTER TABLE performance_kpis ENABLE ROW LEVEL SECURITY;
        CREATE POLICY performance_kpis_select ON performance_kpis
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY performance_kpis_insert ON performance_kpis
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY performance_kpis_update ON performance_kpis
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY performance_kpis_delete ON performance_kpis
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS performance_kpis_delete ON performance_kpis;
        DROP POLICY IF EXISTS performance_kpis_update ON performance_kpis;
        DROP POLICY IF EXISTS performance_kpis_insert ON performance_kpis;
        DROP POLICY IF EXISTS performance_kpis_select ON performance_kpis;
        DROP TABLE IF EXISTS performance_kpis;
        """
    )
