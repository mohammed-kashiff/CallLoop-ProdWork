"""api_usage.actor_id: attribute cost to a user, not just an org (AC-70).

Revision ID: 0041_api_usage_actor
Revises: 0040_audit_log
Create Date: 2026-09-16

api_usage (0002) tracks every outbound Claude/PyAI call's cost, org-scoped
only — an org's total spend is visible, but not which user's actions
drove it. Nullable: background jobs (pollers, webhooks) have no actor to
attach, same reasoning ip_address/actor_email are nullable on audit_log.
Not FK'd to org_members for the same reason impersonation_log's
admin_email isn't — a platform admin or a since-removed member's past
usage must stay attributable even if they're no longer a member.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0041_api_usage_actor"
down_revision: Union[str, Sequence[str], None] = "0040_audit_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE api_usage ADD COLUMN actor_id UUID;
        CREATE INDEX idx_api_usage_actor ON api_usage (actor_id, created_at);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_api_usage_actor;
        ALTER TABLE api_usage DROP COLUMN IF EXISTS actor_id;
        """
    )
