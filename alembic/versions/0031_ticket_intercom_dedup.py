"""tickets.external_id (dedup) + org_credentials.external_account_id (webhook routing).

Revision ID: 0031_ticket_intercom_dedup
Revises: 0030_org_credentials_provider
Create Date: 2026-09-09

IN-5: two gaps found while building Intercom webhook ingestion, neither
covered by earlier Intercom stories.

1. tickets had no external_id at all — re-ingesting the same Intercom
   conversation (Intercom retries webhook delivery on any non-2xx/timeout)
   would create a duplicate ticket every time, with no way to detect it.
   Mirrors calls.external_id's exact pattern from 0001 (org_id, source,
   external_id, unique-when-present).

2. Intercom sends every connected workspace's webhooks to the SAME app-wide
   URL — the payload's own `app_id` is the only thing identifying which
   workspace (and therefore which CallLoop org) an event belongs to.
   org_credentials.external_account_id stores that workspace id (captured
   via GET /me right after OAuth token exchange), indexed for the webhook
   handler's org_id lookup. Nullable/provider-agnostic in case a future
   provider needs the same kind of routing.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0031_ticket_intercom_dedup"
down_revision: Union[str, Sequence[str], None] = "0030_org_credentials_provider"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE tickets ADD COLUMN external_id TEXT;

        CREATE UNIQUE INDEX idx_tickets_org_source_external
            ON tickets (org_id, source, external_id)
            WHERE external_id IS NOT NULL;

        ALTER TABLE org_credentials ADD COLUMN external_account_id TEXT;

        CREATE UNIQUE INDEX idx_org_credentials_provider_external_account
            ON org_credentials (provider, external_account_id)
            WHERE external_account_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_org_credentials_provider_external_account;
        ALTER TABLE org_credentials DROP COLUMN IF EXISTS external_account_id;

        DROP INDEX IF EXISTS idx_tickets_org_source_external;
        ALTER TABLE tickets DROP COLUMN IF EXISTS external_id;
        """
    )
