"""platform_admins: revoke the accidentally-inherited default GRANT.

Revision ID: 0027_platform_admins_revoke_default_grant
Revises: 0026_platform_admins
Create Date: 2026-09-06

0026's own docstring claimed platform_admins is "never granted to
callproof_app" because it has no explicit GRANT statement — that claim
was wrong. 0005_rls.py set `ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO callproof_app`, which
applies automatically to every new table the migration role creates,
platform_admins included. Caught live, right after 0026 was applied to
production, by a test asserting the grant was empty
(test_platform_admins_live_end_to_end).

The practical security guarantee still held even with the grant present:
RLS was enabled with zero policies (0026), and Postgres denies a
non-owner, NOBYPASSRLS role by default when RLS is on and no policy
covers the command — verified directly: SELECT as callproof_app
returned zero rows, INSERT raised InsufficientPrivilege. But the table
should not rely on RLS alone to neutralize a grant it was never supposed
to have — this migration explicitly REVOKEs it, so the actual privileges
match what 0026 always intended and documented.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0027_platform_admins_revoke_default_grant"
down_revision: Union[str, Sequence[str], None] = "0026_platform_admins"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        REVOKE ALL ON platform_admins FROM callproof_app;

        COMMENT ON TABLE platform_admins IS
            'DB-managed additions to the platform-admin allowlist (see '
            'PLATFORM_ADMIN_EMAILS env var, which this extends, not '
            'replaces). Explicitly REVOKEd from callproof_app (0027) — '
            'ALTER DEFAULT PRIVILEGES (0005_rls.py) grants every new '
            'public-schema table to callproof_app by default, so a new '
            'table needs an explicit REVOKE, not just the absence of a '
            'GRANT, to actually be walled off. RLS (enabled, zero '
            'policies) is a second, independent wall — verified that '
            'either one alone already blocks callproof_app. Reach this '
            'table only via is_platform_admin_email() / '
            'list_platform_admins() / add_platform_admin() / '
            'remove_platform_admin().';
        """
    )


def downgrade() -> None:
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_admins TO callproof_app;"
    )
