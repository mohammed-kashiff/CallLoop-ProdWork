"""org_members first/last name + admin-only org_directory view.

Revision ID: 0011_org_members_names
Revises: 0010_orgs_domain
Create Date: 2026-08-27

Names are nullable — existing rows stay NULL (no backfill).
org_directory joins auth.users for email. It is a human/admin query surface
(SQL editor), not an API. Do not GRANT it to callproof_app. CI Postgres has
no auth schema — the view is skipped there; columns still apply.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0011_org_members_names"
down_revision: Union[str, Sequence[str], None] = "0010_orgs_domain"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE org_members ADD COLUMN first_name TEXT;
        ALTER TABLE org_members ADD COLUMN last_name TEXT;

        COMMENT ON COLUMN org_members.first_name IS
            'Captured once at signup from JWT user_metadata. Not updated on login.';
        COMMENT ON COLUMN org_members.last_name IS
            'Captured once at signup from JWT user_metadata. Not updated on login.';
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('auth.users') IS NOT NULL THEN
            EXECUTE $view$
              CREATE VIEW org_directory AS
              SELECT om.user_id, u.email, om.first_name, om.last_name, om.role,
                     om.org_id, o.name AS org_name, om.created_at
              FROM org_members om
              JOIN auth.users u ON u.id = om.user_id
              JOIN orgs o ON o.id = om.org_id
            $view$;
            COMMENT ON VIEW org_directory IS
              'Admin directory across orgs. Do not GRANT to callproof_app. '
              'Do not expose via an API without per-org filtering.';
            REVOKE ALL ON org_directory FROM PUBLIC;
            REVOKE ALL ON org_directory FROM callproof_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP VIEW IF EXISTS org_directory;
        ALTER TABLE org_members DROP COLUMN IF EXISTS last_name;
        ALTER TABLE org_members DROP COLUMN IF EXISTS first_name;
        """
    )
