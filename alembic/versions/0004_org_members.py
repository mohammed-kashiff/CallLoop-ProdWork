"""org_members: user_id is Supabase JWT sub (CL-8). Auth only — not query scoping.

Revision ID: 0004_org_members
Revises: 0003_rubrics_audits
Create Date: 2026-08-24
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0004_org_members"
down_revision: Union[str, Sequence[str], None] = "0003_rubrics_audits"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE org_members (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            user_id UUID NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'member')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (org_id, user_id),
            UNIQUE (user_id)
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS org_members;
        """
    )
