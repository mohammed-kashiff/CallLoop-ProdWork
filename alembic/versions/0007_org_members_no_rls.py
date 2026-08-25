"""Disable RLS on org_members (CL-10). Signup bootstrap must insert without an org GUC.

Revision ID: 0007_org_members_no_rls
Revises: 0006_rls_role_grant
Create Date: 2026-08-25

Supabase (or the Table Editor) enabled RLS on org_members with no policies.
That is deny-all for callproof_app. It was invisible while the API connected as
postgres (BYPASSRLS). After SET LOCAL ROLE, INSERT at first login fails with
'new row violates row-level security policy for table org_members'.
This table stays without RLS: membership lookup happens before org GUC is set.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0007_org_members_no_rls"
down_revision: Union[str, Sequence[str], None] = "0006_rls_role_grant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE org_members DISABLE ROW LEVEL SECURITY;")


def downgrade() -> None:
    op.execute("ALTER TABLE org_members ENABLE ROW LEVEL SECURITY;")
