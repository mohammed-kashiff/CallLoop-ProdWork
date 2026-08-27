"""org_id_for_name() so admin provisioning joins a same-name org (AC-7).

Revision ID: 0015_org_id_for_name
Revises: 0014_org_features_write
Create Date: 2026-08-28

orgs.name has no uniqueness constraint (unlike domain) — this is a lookup
helper for admin_provision.py only, not a new invariant on the table.
Exact match, case-insensitive, trimmed. Excludes DEFAULT_ORG_ID explicitly
so a name collision can never place a real user in the placeholder org.
SECURITY DEFINER for the same reason org_id_for_domain() is: orgs has RLS,
the admin doesn't know the target id in advance, so a plain scoped
connection can't search across orgs by name. Do not broaden this function.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from backend.org_ids import DEFAULT_ORG_ID

revision: str = "0015_org_id_for_name"
down_revision: Union[str, Sequence[str], None] = "0014_org_features_write"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.org_id_for_name(p_name text) RETURNS uuid
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public
        AS $$
            SELECT id FROM orgs
            WHERE lower(btrim(name)) = lower(btrim(p_name))
              AND id <> '{DEFAULT_ORG_ID}'::uuid
            ORDER BY created_at ASC
            LIMIT 1
        $$;

        COMMENT ON FUNCTION public.org_id_for_name(text) IS
            'Admin-provisioning-only lookup: exact, case-insensitive, trimmed
             name match, oldest org wins if duplicates exist, DEFAULT_ORG_ID
             excluded. Do not broaden to fuzzy matching or other columns.';

        GRANT EXECUTE ON FUNCTION public.org_id_for_name(text) TO callproof_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.org_id_for_name(text);
        """
    )
