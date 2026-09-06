"""product_events: append-only, org-scoped usage telemetry (AC-43).

Revision ID: 0028_product_events
Revises: 0027_platform_admins_revoke
Create Date: 2026-09-07

Source: CallLoop-Observability-PRD (Telemetry & Logging), §4/§7. Same
append-only convention as password_reset_events (0018) / org_features_history
(0016) / impersonation_log (0020) / call_pipeline_events (0021): INSERT +
SELECT only, no UPDATE/DELETE grant or policy. RLS is org-scoped via
callproof_current_org_id() (not the ungranted org_directory pattern) since
this is written from ordinary per-request tenant-scoped connections, not a
bypass connection.

properties is a small JSONB blob — structural facts only (counts, ids,
booleans, small enums). Per the PRD's firm rule: never transcript text,
ticket message content, screenshot content, or customer PII. Enforced at
the call-site level (backend/product_events.py), not by this schema, the
same way audits.findings' evidence-text trust boundary is enforced by
application code rather than a DB constraint.

user_id is nullable — some future event source may not have one (a
webhook-driven ingestion, for instance), though every event wired in AC-44
today does have one from the verified JWT.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0028_product_events"
down_revision: Union[str, Sequence[str], None] = "0027_platform_admins_revoke"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE product_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            user_id UUID,
            event_name TEXT NOT NULL,
            properties JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_product_events_org_created
            ON product_events (org_id, created_at DESC);
        CREATE INDEX idx_product_events_name_created
            ON product_events (event_name, created_at DESC);

        COMMENT ON TABLE product_events IS
            'Append-only usage telemetry (AC-43/observability PRD). One row '
            'per tracked product event (call_uploaded, rubric_saved, '
            'session_started, ...). properties is structural facts only — '
            'never transcript/ticket/screenshot content or PII.';

        GRANT SELECT, INSERT ON product_events TO callproof_app;

        ALTER TABLE product_events ENABLE ROW LEVEL SECURITY;
        CREATE POLICY product_events_select ON product_events
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY product_events_insert ON product_events
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS product_events_insert ON product_events;
        DROP POLICY IF EXISTS product_events_select ON product_events;
        DROP TABLE IF EXISTS product_events;
        """
    )
