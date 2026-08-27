"""org_members.short_id sequence + org_directory includes it.

Revision ID: 0012_org_members_short_id
Revises: 0011_org_members_names
Create Date: 2026-08-27

Plain sequence from 100000 (not random). INSERT does not name short_id —
DEFAULT nextval fills it. GRANT USAGE, SELECT on the sequence to
callproof_app is required: without it, SET ROLE callproof_app inserts fail
the same way Vault grants did. Do not drop that GRANT.

org_directory is admin SQL only. Recreate + REVOKE so default privileges
cannot leak it to callproof_app. CI has no auth.users — view is skipped.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0012_org_members_short_id"
down_revision: Union[str, Sequence[str], None] = "0011_org_members_names"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SEQUENCE org_members_short_id_seq START 100000;
        ALTER TABLE org_members ADD COLUMN short_id INTEGER UNIQUE
          DEFAULT nextval('org_members_short_id_seq');
        ALTER SEQUENCE org_members_short_id_seq OWNED BY org_members.short_id;
        GRANT USAGE, SELECT ON SEQUENCE org_members_short_id_seq TO callproof_app;
        UPDATE org_members SET short_id = nextval('org_members_short_id_seq')
        WHERE short_id IS NULL;

        COMMENT ON COLUMN org_members.short_id IS
            'Public-facing integer from org_members_short_id_seq (starts 100000). '
            'Assigned by DEFAULT nextval; not set in application INSERT.';
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('auth.users') IS NOT NULL THEN
            EXECUTE $view$
              CREATE OR REPLACE VIEW org_directory AS
              SELECT om.user_id, u.email, om.first_name, om.last_name, om.role,
                     om.org_id, o.name AS org_name, om.created_at, om.short_id
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
        ALTER TABLE org_members DROP COLUMN IF EXISTS short_id;
        DROP SEQUENCE IF EXISTS org_members_short_id_seq;
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
