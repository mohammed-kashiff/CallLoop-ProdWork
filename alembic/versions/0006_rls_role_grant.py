"""GRANT callproof_app to the DB login so SET ROLE works (CL-10).

Revision ID: 0006_rls_role_grant
Revises: 0005_rls
Create Date: 2026-08-25

0005 created callproof_app as NOBYPASSRLS. Supabase pooler postgres is not a
superuser, so SET ROLE was denied until this grant. Fresh installs get the
same GRANT in 0005; this revision is for databases that already applied 0005.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0006_rls_role_grant"
down_revision: Union[str, Sequence[str], None] = "0005_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        GRANT callproof_app TO CURRENT_USER;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                GRANT callproof_app TO postgres;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                REVOKE callproof_app FROM postgres;
            END IF;
        EXCEPTION
            WHEN undefined_object THEN
                NULL;
        END
        $$;
        """
    )
