"""org_credentials.provider: generic format check, not a JustCall-only enum.

Revision ID: 0030_org_credentials_provider
Revises: 0029_admin_search_orgs
Create Date: 2026-09-09

IN-4: org_vault.py is now provider-agnostic (put_credential/load_credential),
so the DB-level CHECK (provider IN ('justcall')) from 0009 would reject the
very first non-JustCall write (Intercom, IN-3) with no application-level
error to explain why. Replaced with a lightweight format check — lowercase,
starts with a letter, letters/digits/underscore only — matching org_vault.py's
own _PROVIDER_NAME_RE validation, so a bad provider string still fails loudly,
just without a migration required per new integration.

Revision id is deliberately kept <=32 chars: alembic_version.version_num is
VARCHAR(32) (Alembic's own default) — the first name for this migration,
"0030_org_credentials_generic_provider" (37 chars), truncation-errored the
version bump on deploy and rolled the whole migration back. Same lesson
applies to every future revision id in this repo, not just this one.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0030_org_credentials_provider"
down_revision: Union[str, Sequence[str], None] = "0029_admin_search_orgs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE org_credentials
            DROP CONSTRAINT org_credentials_provider_check;
        ALTER TABLE org_credentials
            ADD CONSTRAINT org_credentials_provider_check
                CHECK (provider ~ '^[a-z][a-z0-9_]*$');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE org_credentials
            DROP CONSTRAINT org_credentials_provider_check;
        ALTER TABLE org_credentials
            ADD CONSTRAINT org_credentials_provider_check
                CHECK (provider IN ('justcall'));
        """
    )
