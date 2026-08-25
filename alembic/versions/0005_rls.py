"""RLS on tenant tables + non-bypass API role (CL-10). Policies in Alembic only.

Revision ID: 0005_rls
Revises: 0004_org_members
Create Date: 2026-08-25

Row Level Security is the second tenant layer behind application org_id filters.
Policies key on app.current_org_id (SET LOCAL by the API after JWT verification).

API sessions must not bypass RLS. DATABASE_URL may still be the postgres URI
(Alembic needs a BYPASSRLS role). db.connection() SET LOCAL ROLE callproof_app,
which is NOLOGIN NOSUPERUSER NOBYPASSRLS. Verify current_user / rolbypassrls on
an API connection — not on a raw postgres shell. bypass_rls=True is for
one-off backfill only. Do not toggle RLS in the Table Editor.

org_members is not RLS'd: first-user claim reads it before the org GUC exists.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0005_rls"
down_revision: Union[str, Sequence[str], None] = "0004_org_members"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_RLS_TABLES = ("orgs", "calls", "segments", "audits", "rubrics", "api_usage")


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.callproof_current_org_id()
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        AS $$
        DECLARE
            raw text;
        BEGIN
            raw := nullif(current_setting('app.current_org_id', true), '');
            IF raw IS NULL THEN
                RETURN NULL;
            END IF;
            RETURN raw::uuid;
        EXCEPTION
            WHEN invalid_text_representation THEN
                RETURN NULL;
        END;
        $$;

        COMMENT ON FUNCTION public.callproof_current_org_id() IS
            'RLS helper. Reads app.current_org_id set by the API after JWT verification. '
            'Empty or invalid → NULL (deny). Superuser and BYPASSRLS (service_role) skip RLS; '
            'those roles are for Alembic and backfill only. API uses SET LOCAL ROLE callproof_app.';

        ALTER FUNCTION public.callproof_current_org_id()
            SET search_path = pg_catalog, public;

        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'callproof_app') THEN
                CREATE ROLE callproof_app
                    NOLOGIN
                    NOSUPERUSER
                    NOCREATEDB
                    NOCREATEROLE
                    NOINHERIT
                    NOBYPASSRLS;
            ELSE
                ALTER ROLE callproof_app WITH NOLOGIN NOSUPERUSER NOBYPASSRLS;
            END IF;
        END
        $$;

        COMMENT ON ROLE callproof_app IS
            'API runtime role. SET LOCAL ROLE from db.connection(). NOBYPASSRLS. '
            'postgres/service_role remain for Alembic and backfill only.';

        -- Pooler postgres is often not superuser; SET ROLE requires membership.
        GRANT callproof_app TO CURRENT_USER;
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
                GRANT callproof_app TO postgres;
            END IF;
        END
        $$;

        GRANT USAGE ON SCHEMA public TO callproof_app;
        GRANT EXECUTE ON FUNCTION public.callproof_current_org_id() TO callproof_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON
            orgs, calls, segments, audits, rubrics, api_usage, org_members
            TO callproof_app;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO callproof_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO callproof_app;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public
            GRANT USAGE, SELECT ON SEQUENCES TO callproof_app;

        ALTER TABLE orgs ENABLE ROW LEVEL SECURITY;
        CREATE POLICY orgs_select ON orgs
            FOR SELECT USING (id = public.callproof_current_org_id());
        CREATE POLICY orgs_insert ON orgs
            FOR INSERT WITH CHECK (id = public.callproof_current_org_id());
        CREATE POLICY orgs_update ON orgs
            FOR UPDATE USING (id = public.callproof_current_org_id())
            WITH CHECK (id = public.callproof_current_org_id());
        CREATE POLICY orgs_delete ON orgs
            FOR DELETE USING (id = public.callproof_current_org_id());

        ALTER TABLE calls ENABLE ROW LEVEL SECURITY;
        CREATE POLICY calls_select ON calls
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY calls_insert ON calls
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY calls_update ON calls
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY calls_delete ON calls
            FOR DELETE USING (org_id = public.callproof_current_org_id());

        ALTER TABLE segments ENABLE ROW LEVEL SECURITY;
        CREATE POLICY segments_select ON segments
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY segments_insert ON segments
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY segments_update ON segments
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY segments_delete ON segments
            FOR DELETE USING (org_id = public.callproof_current_org_id());

        ALTER TABLE audits ENABLE ROW LEVEL SECURITY;
        CREATE POLICY audits_select ON audits
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY audits_insert ON audits
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY audits_update ON audits
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY audits_delete ON audits
            FOR DELETE USING (org_id = public.callproof_current_org_id());

        ALTER TABLE rubrics ENABLE ROW LEVEL SECURITY;
        CREATE POLICY rubrics_select ON rubrics
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY rubrics_insert ON rubrics
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY rubrics_update ON rubrics
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY rubrics_delete ON rubrics
            FOR DELETE USING (org_id = public.callproof_current_org_id());

        ALTER TABLE api_usage ENABLE ROW LEVEL SECURITY;
        CREATE POLICY api_usage_select ON api_usage
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY api_usage_insert ON api_usage
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY api_usage_update ON api_usage
            FOR UPDATE USING (org_id = public.callproof_current_org_id())
            WITH CHECK (org_id = public.callproof_current_org_id());
        CREATE POLICY api_usage_delete ON api_usage
            FOR DELETE USING (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    for table in reversed(_RLS_TABLES):
        op.execute(
            f"""
            DROP POLICY IF EXISTS {table}_select ON {table};
            DROP POLICY IF EXISTS {table}_insert ON {table};
            DROP POLICY IF EXISTS {table}_update ON {table};
            DROP POLICY IF EXISTS {table}_delete ON {table};
            ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;
            """
        )
    op.execute(
        """
        REVOKE ALL ON orgs, calls, segments, audits, rubrics, api_usage, org_members
            FROM callproof_app;
        DROP FUNCTION IF EXISTS public.callproof_current_org_id();
        DROP ROLE IF EXISTS callproof_app;
        """
    )
