"""call_pipeline_events: append-only, per-call audit trail for the whole
upload -> transcribe -> score -> serve pipeline (CC-2).

Revision ID: 0021_call_pipeline_events
Revises: 0020_impersonation_log
Create Date: 2026-09-04

Every meaningful stage of a call's life gets one row: upload received,
transcription started/complete/failed, scoring started, one row per
criterion dispatched to the LLM (or a deterministic check) and one for its
result, recap started/succeeded/failed, the final audit_complete/failed,
and result_served every time the audit is actually returned to a client
(the practical, backend-observable proxy for "displayed" — this process
has no way to know when something rendered on a screen).

Same append-only convention as password_reset_events (0018) /
org_features_history (0016) / impersonation_log (0020): INSERT + SELECT
only, no UPDATE/DELETE grant or policy. RLS is org-scoped via
callproof_current_org_id() like those tables (not the ungranted
org_directory pattern) since this is written from ordinary per-request
tenant-scoped connections, not a bypass connection.

detail is JSONB, freeform per stage (dimension id/method/weight for a
criterion event, mode/segment count for transcription, etc). error is a
plain string, populated only on status='failed' rows — same trust
boundary as audits.findings, which already stores transcript-derived
evidence text for this same call; nothing new is exposed here that isn't
already at rest elsewhere for the same call_id.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0021_call_pipeline_events"
down_revision: Union[str, Sequence[str], None] = "0020_impersonation_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE call_pipeline_events (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE CASCADE,
            call_id INTEGER NOT NULL,
            stage TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('started', 'succeeded', 'failed')),
            detail JSONB,
            error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_call_pipeline_events_call
            ON call_pipeline_events (call_id, created_at);
        CREATE INDEX idx_call_pipeline_events_org_created
            ON call_pipeline_events (org_id, created_at DESC);

        COMMENT ON TABLE call_pipeline_events IS
            'Append-only per-call pipeline trail: upload, transcription, '
            'per-criterion scoring, recap, final audit result, and every '
            'time the audit was served to a client. One row per stage '
            'transition, including every failure with its cause.';

        GRANT SELECT, INSERT ON call_pipeline_events TO callproof_app;

        ALTER TABLE call_pipeline_events ENABLE ROW LEVEL SECURITY;
        CREATE POLICY call_pipeline_events_select ON call_pipeline_events
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY call_pipeline_events_insert ON call_pipeline_events
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS call_pipeline_events_insert ON call_pipeline_events;
        DROP POLICY IF EXISTS call_pipeline_events_select ON call_pipeline_events;
        DROP TABLE IF EXISTS call_pipeline_events;
        """
    )
