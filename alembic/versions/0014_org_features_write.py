"""org_features write policies + admin directory search (AC-5).

Revision ID: 0014_org_features_write
Revises: 0013_org_features
Create Date: 2026-08-28

INSERT/UPDATE policies key on callproof_current_org_id() so AC-5 can upsert
via apply_tenant_gucs(target_org) without a bypass connection. Still no
cross-org writes: GUC must match the row's org_id.

admin_search_directory is SECURITY DEFINER so the platform-admin API can
search org_directory without GRANTing that view to callproof_app. The HTTP
handler still calls require_platform_admin first. CI has no auth.users —
the function is skipped there.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0014_org_features_write"
down_revision: Union[str, Sequence[str], None] = "0013_org_features"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE POLICY org_features_insert ON org_features
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY org_features_update ON org_features
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('public.org_directory') IS NOT NULL THEN
            EXECUTE $fn$
              CREATE OR REPLACE FUNCTION public.admin_search_directory(p_q text)
              RETURNS TABLE (
                user_id uuid,
                email text,
                first_name text,
                last_name text,
                role text,
                org_id uuid,
                org_name text,
                created_at timestamptz,
                short_id integer,
                first_seen timestamptz,
                last_sign_in_at timestamptz
              )
              LANGUAGE sql
              STABLE
              SECURITY DEFINER
              SET search_path = public
              AS $body$
                SELECT d.user_id, d.email, d.first_name, d.last_name, d.role,
                       d.org_id, d.org_name, d.created_at, d.short_id,
                       d.first_seen, d.last_sign_in_at
                FROM org_directory d
                WHERE coalesce(btrim(p_q), '') = ''
                   OR position(lower(p_q) in lower(coalesce(d.email, ''))) > 0
                   OR position(lower(p_q) in lower(coalesce(d.first_name, ''))) > 0
                   OR position(lower(p_q) in lower(coalesce(d.last_name, ''))) > 0
                   OR position(
                        lower(p_q) in lower(
                          coalesce(d.first_name, '') || ' ' || coalesce(d.last_name, '')
                        )
                      ) > 0
                   OR position(lower(p_q) in d.org_id::text) > 0
                   OR position(lower(p_q) in d.user_id::text) > 0
                   OR position(lower(p_q) in d.short_id::text) > 0
                   OR position(lower(p_q) in lower(coalesce(d.org_name, ''))) > 0
                ORDER BY d.last_sign_in_at DESC NULLS LAST,
                         d.first_seen DESC NULLS LAST
                LIMIT 50
              $body$
            $fn$;
            COMMENT ON FUNCTION public.admin_search_directory(text) IS
              'Platform-admin directory search. HTTP must still call '
              'require_platform_admin. Do not GRANT org_directory to callproof_app.';
            REVOKE ALL ON FUNCTION public.admin_search_directory(text) FROM PUBLIC;
            GRANT EXECUTE ON FUNCTION public.admin_search_directory(text) TO callproof_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.admin_search_directory(text);
        DROP POLICY IF EXISTS org_features_update ON org_features;
        DROP POLICY IF EXISTS org_features_insert ON org_features;
        """
    )
