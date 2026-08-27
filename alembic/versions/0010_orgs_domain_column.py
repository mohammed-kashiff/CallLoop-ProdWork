"""orgs.domain for same-company signup matching (nullable unique).

Revision ID: 0010_orgs_domain
Revises: 0009_org_vault_justcall
Create Date: 2026-08-27

Do not backfill the placeholder org. Public-provider signups leave domain NULL
so they never auto-join each other. UNIQUE allows multiple NULLs.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0010_orgs_domain"
down_revision: Union[str, Sequence[str], None] = "0009_org_vault_justcall"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE orgs ADD COLUMN domain TEXT;
        ALTER TABLE orgs ADD CONSTRAINT orgs_domain_key UNIQUE (domain);
        COMMENT ON COLUMN orgs.domain IS
            'Email domain for auto-join. NULL for public-provider and placeholder orgs.';

        CREATE FUNCTION public.org_id_for_domain(p_domain text) RETURNS uuid
        LANGUAGE sql SECURITY DEFINER SET search_path = public AS
        $$ SELECT id FROM orgs WHERE domain = p_domain $$;

        COMMENT ON FUNCTION public.org_id_for_domain(text) IS
            'Narrow signup-only lookup: returns only id for an exact domain match.
             Do not broaden — this is the one sanctioned RLS bypass for org lookup
             by domain. Never add other columns or a wildcard match to it.';

        GRANT EXECUTE ON FUNCTION public.org_id_for_domain(text) TO callproof_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS public.org_id_for_domain(text);
        ALTER TABLE orgs DROP CONSTRAINT IF EXISTS orgs_domain_key;
        ALTER TABLE orgs DROP COLUMN IF EXISTS domain;
        """
    )
