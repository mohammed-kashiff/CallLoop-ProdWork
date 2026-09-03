"""calls.deleted_at / deleted_by: soft-delete for calls (AC-17).

Revision ID: 0019_call_soft_delete
Revises: 0018_password_reset_events
Create Date: 2026-09-03

Two independent delete features share this column pair: the existing
org-wide "Clear cache" (now gated behind a default-off feature flag,
platform-admin only) and a new user-facing per-call delete. Both only
remove the audio recording (via the existing playback-audio store) and
soft-delete the call row; segments/audits/raw_json are kept, since
transcript text is a few KB per call versus multi-MB audio.

Nullable, no backfill — existing calls were never deleted. FK to
org_members(user_id), same pattern as uploaded_by/requested_by (0017).
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0019_call_soft_delete"
down_revision: Union[str, Sequence[str], None] = "0018_password_reset_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE calls
            ADD COLUMN deleted_at TIMESTAMPTZ,
            ADD COLUMN deleted_by UUID REFERENCES org_members (user_id);

        CREATE INDEX idx_calls_org_deleted ON calls (org_id, deleted_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_calls_org_deleted;
        ALTER TABLE calls DROP COLUMN IF EXISTS deleted_by;
        ALTER TABLE calls DROP COLUMN IF EXISTS deleted_at;
        """
    )
