"""AC-56/AC-57: widen org_members.role to allow 'manager'.

Revision ID: 0038_org_members_manager_role
Revises: 0037_ticket_msgs_agent_update
Create Date: 2026-09-16

org_members.role has had a hard CHECK (role IN ('owner', 'member')) since
0004_org_members.py. Adding a real Manager tier (Roles PRD, 2026-09-08)
needs a real migration, not an app-layer-only change — confirmed live:
the constraint is named org_members_role_check (Postgres's default name
for an unnamed inline CHECK).

Downgrade limitation: re-adding the narrower constraint fails if any row
already has role = 'manager' at downgrade time — same class of limitation
as any role-set narrowing. Not worked around here; a downgrade after
Manager rows exist requires demoting them first.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0038_org_members_manager_role"
down_revision: Union[str, Sequence[str], None] = "0037_ticket_msgs_agent_update"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE org_members DROP CONSTRAINT org_members_role_check;
        ALTER TABLE org_members ADD CONSTRAINT org_members_role_check
            CHECK (role IN ('owner', 'manager', 'member'));
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE org_members DROP CONSTRAINT org_members_role_check;
        ALTER TABLE org_members ADD CONSTRAINT org_members_role_check
            CHECK (role IN ('owner', 'member'));
        """
    )
