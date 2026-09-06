"""admin_search_orgs — one row per org for Command Center's directory table.

Revision ID: 0029_admin_search_orgs
Revises: 0028_product_events
Create Date: 2026-09-07

AC-33 (Command Center Redesign PRD, AC-31 epic). admin_search_directory()
returns one row per org_members row — an org with 3 members shows up 3
times, unusable for a "one row per org" directory table. This is a second,
independent SECURITY DEFINER function alongside it, not a replacement:
admin_search_directory keeps its exact current per-member shape (the
Members tab, AC-34, still needs that). Queries orgs/org_members directly
rather than through org_directory, since org_directory's created_at column
is the *membership* row's created_at, not the org's own — this function
needs the org's real creation date (orgs.created_at).

Same convention as admin_search_directory/org_id_for_name/platform_admins:
STABLE, SET search_path = public, REVOKE ALL FROM PUBLIC then GRANT EXECUTE
to callproof_app only. CI has no auth.users, but this function only reads
orgs/org_members (no auth schema dependency), so unlike admin_search_directory
it does not need an org_directory existence guard.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0029_admin_search_orgs"
down_revision: Union[str, Sequence[str], None] = "0028_product_events"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.admin_search_orgs(p_q text)
        RETURNS TABLE (
            org_id uuid,
            org_name text,
            short_ids integer[],
            member_count bigint,
            created_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT o.id AS org_id,
                   o.name AS org_name,
                   array_remove(array_agg(om.short_id ORDER BY om.short_id), NULL) AS short_ids,
                   count(om.user_id) AS member_count,
                   o.created_at
            FROM orgs o
            LEFT JOIN org_members om ON om.org_id = o.id
            WHERE coalesce(btrim(p_q), '') = ''
               OR position(lower(p_q) in lower(o.name)) > 0
               OR position(lower(p_q) in o.id::text) > 0
               OR EXISTS (
                    SELECT 1 FROM org_members om2
                    WHERE om2.org_id = o.id
                      AND position(lower(p_q) in om2.short_id::text) > 0
                  )
            GROUP BY o.id, o.name, o.created_at
            ORDER BY o.created_at DESC
            LIMIT 50
        $$;

        COMMENT ON FUNCTION public.admin_search_orgs(text) IS
            'Platform-admin org directory search (one row per org). HTTP '
            'must still call require_platform_admin. Does not replace '
            'admin_search_directory, which the Members tab still needs.';
        REVOKE ALL ON FUNCTION public.admin_search_orgs(text) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION public.admin_search_orgs(text) TO callproof_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS public.admin_search_orgs(text);")
