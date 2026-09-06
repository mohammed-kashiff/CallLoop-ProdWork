"""platform_admins: DB-managed platform-admin allowlist.

Revision ID: 0026_platform_admins
Revises: 0025_ticket_agent_aliases
Create Date: 2026-09-06

Platform admin is CallLoop-internal staff only, never a customer — today
it is a static PLATFORM_ADMIN_EMAILS env var (auth.is_platform_admin()),
which means adding someone needs a Render env change + restart. This adds
a way to grant it from the UI (Command Center > Platform Admins) without
touching env vars, while keeping the blast radius as small as the static
allowlist it extends.

platform_admins has NO org_id — platform admin is cross-tenant by
definition, so the usual `org_id = callproof_current_org_id()` RLS
pattern doesn't apply here. This table has no explicit GRANT in this
migration, and RLS is enabled with ZERO policies so a non-owner,
NOBYPASSRLS role (callproof_app) is denied by default on every command.

CORRECTION (see 0027): the "no explicit GRANT means no privileges" half
of that plan was wrong. 0005_rls.py's `ALTER DEFAULT PRIVILEGES IN
SCHEMA public GRANT ... ON TABLES TO callproof_app` applies to every new
table automatically, platform_admins included — 0011's org_directory
view already had to REVOKE explicitly for exactly this reason, a
precedent this migration should have followed and didn't. The RLS wall
held regardless (verified live: SELECT as callproof_app returned zero
rows, INSERT raised InsufficientPrivilege), but 0027 explicitly REVOKEs
the inherited grant too, so actual privileges match what this migration
always intended rather than relying on RLS alone to neutralize a grant
that shouldn't exist.

Every read/write goes through one of four narrow SECURITY DEFINER
functions below regardless, mirroring the existing
admin_search_directory()/org_id_for_name() pattern (0014/0015) — SQL
LANGUAGE, STABLE, SET search_path = public, REVOKE ALL FROM PUBLIC then
GRANT EXECUTE to callproof_app specifically, so callproof_app can call
these four exact operations and nothing else on this table.

is_platform_admin_email() is called once per request, folded into the
existing ensure_membership() round trip (auth.py) — no new per-request
DB connection, just one more cheap indexed lookup in the connection
that already runs on every authenticated request. list_platform_admins()
exposes the full list with no restriction of its own; the HTTP handler
(auth.require_platform_admin) is what keeps it away from anyone but an
existing platform admin, same convention as admin_search_directory's own
docstring states explicitly.

The static PLATFORM_ADMIN_EMAILS env var is untouched and still checked
(auth.is_platform_admin() ORs both) — this table is purely additive, not
a replacement, so existing admins are never affected by this migration.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0026_platform_admins"
down_revision: Union[str, Sequence[str], None] = "0025_ticket_agent_aliases"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE platform_admins (
            email TEXT PRIMARY KEY,
            added_by TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        COMMENT ON TABLE platform_admins IS
            'DB-managed additions to the platform-admin allowlist (see '
            'PLATFORM_ADMIN_EMAILS env var, which this extends, not '
            'replaces). Deliberately NOT granted to callproof_app — reach '
            'it only via is_platform_admin_email() / list_platform_admins() '
            '/ add_platform_admin() / remove_platform_admin() below.';

        ALTER TABLE platform_admins ENABLE ROW LEVEL SECURITY;
        -- No policies defined on purpose: with zero grants to
        -- callproof_app this is redundant today, but if a future
        -- migration ever mistakenly GRANTs this table, RLS with no
        -- policies still denies every row by default.

        CREATE FUNCTION public.is_platform_admin_email(p_email text) RETURNS boolean
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT EXISTS (
                SELECT 1 FROM platform_admins
                WHERE email = lower(btrim(p_email))
            )
        $$;

        CREATE FUNCTION public.list_platform_admins()
        RETURNS TABLE (email text, added_by text, created_at timestamptz)
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT email, added_by, created_at
            FROM platform_admins
            ORDER BY created_at
        $$;

        CREATE FUNCTION public.add_platform_admin(p_email text, p_added_by text) RETURNS void
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            INSERT INTO platform_admins (email, added_by)
            VALUES (lower(btrim(p_email)), p_added_by)
            ON CONFLICT (email) DO NOTHING
        $$;

        CREATE FUNCTION public.remove_platform_admin(p_email text) RETURNS void
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = public
        AS $$
            DELETE FROM platform_admins WHERE email = lower(btrim(p_email))
        $$;

        COMMENT ON FUNCTION public.is_platform_admin_email(text) IS
            'SECURITY DEFINER: platform_admins is never granted to '
            'callproof_app. Called once per request from '
            'auth.ensure_membership(), ORed with PLATFORM_ADMIN_EMAILS.';
        COMMENT ON FUNCTION public.list_platform_admins() IS
            'SECURITY DEFINER, no restriction of its own — the HTTP '
            'handler (auth.require_platform_admin) is what keeps this '
            'away from anyone but an existing platform admin.';
        COMMENT ON FUNCTION public.add_platform_admin(text, text) IS
            'SECURITY DEFINER. Caller must already have passed '
            'require_platform_admin — this function does no permission '
            'check of its own.';
        COMMENT ON FUNCTION public.remove_platform_admin(text) IS
            'SECURITY DEFINER. Caller must already have passed '
            'require_platform_admin — this function does no permission '
            'check of its own.';

        REVOKE ALL ON FUNCTION public.is_platform_admin_email(text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.list_platform_admins() FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.add_platform_admin(text, text) FROM PUBLIC;
        REVOKE ALL ON FUNCTION public.remove_platform_admin(text) FROM PUBLIC;

        GRANT EXECUTE ON FUNCTION public.is_platform_admin_email(text) TO callproof_app;
        GRANT EXECUTE ON FUNCTION public.list_platform_admins() TO callproof_app;
        GRANT EXECUTE ON FUNCTION public.add_platform_admin(text, text) TO callproof_app;
        GRANT EXECUTE ON FUNCTION public.remove_platform_admin(text) TO callproof_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.remove_platform_admin(text);
        DROP FUNCTION IF EXISTS public.add_platform_admin(text, text);
        DROP FUNCTION IF EXISTS public.list_platform_admins();
        DROP FUNCTION IF EXISTS public.is_platform_admin_email(text);
        DROP TABLE IF EXISTS platform_admins;
        """
    )
