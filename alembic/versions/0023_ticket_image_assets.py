"""Ticket image assets: private Storage bucket + metadata table (TA-5).

Revision ID: 0023_ticket_image_assets
Revises: 0022_tickets
Create Date: 2026-09-05

TA-5: a screenshot embedded in a ticket PDF is extracted at ingest time,
described with one Claude vision call (injected into ticket_messages as a
normal turn — no schema change needed there, see ticket_ingest.py), and
the original image bytes are kept as a viewable asset so a reviewer can
check the AI's description against the real picture.

Same private-bucket pattern as call-audio (0008_storage_audio_bucket):
no-op on vanilla/CI Postgres, a real private bucket on Supabase. Signed
URLs are minted server-side with the service role; no public/authenticated
read policy here.

ticket_message_assets is metadata only (no image bytes in Postgres) — one
row per stored image, FK'd on (ticket_id, seq) rather than ticket_messages.id
so this doesn't need that insert's returned id. Append-only, same
SELECT/INSERT-only + RLS shape as ticket_messages itself.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0023_ticket_image_assets"
down_revision: Union[str, Sequence[str], None] = "0022_tickets"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('storage.buckets') IS NOT NULL THEN
            INSERT INTO storage.buckets (id, name, public)
            VALUES ('ticket-images', 'ticket-images', false)
            ON CONFLICT (id) DO UPDATE SET public = false;
          END IF;
        END $$;

        CREATE TABLE ticket_message_assets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            ticket_id UUID NOT NULL,
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            seq INTEGER NOT NULL,
            content_type TEXT NOT NULL DEFAULT 'image/png',
            width INTEGER NOT NULL,
            height INTEGER NOT NULL,
            storage_key TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (ticket_id, seq),
            FOREIGN KEY (ticket_id, seq)
                REFERENCES ticket_messages (ticket_id, seq) ON DELETE CASCADE
        );

        CREATE INDEX idx_ticket_message_assets_org_id
            ON ticket_message_assets (org_id);
        CREATE INDEX idx_ticket_message_assets_ticket_id
            ON ticket_message_assets (ticket_id);

        COMMENT ON TABLE ticket_message_assets IS
            'TA-5. One row per ticket screenshot kept as a viewable asset. '
            'No image bytes here — storage_key points into the private '
            'ticket-images Storage bucket. Append-only.';

        GRANT SELECT, INSERT ON ticket_message_assets TO callproof_app;

        ALTER TABLE ticket_message_assets ENABLE ROW LEVEL SECURITY;
        CREATE POLICY ticket_message_assets_select ON ticket_message_assets
            FOR SELECT USING (org_id = public.callproof_current_org_id());
        CREATE POLICY ticket_message_assets_insert ON ticket_message_assets
            FOR INSERT WITH CHECK (org_id = public.callproof_current_org_id());
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS ticket_message_assets_insert ON ticket_message_assets;
        DROP POLICY IF EXISTS ticket_message_assets_select ON ticket_message_assets;
        DROP TABLE IF EXISTS ticket_message_assets;

        DO $$
        BEGIN
          IF to_regclass('storage.buckets') IS NOT NULL
             AND to_regclass('storage.objects') IS NOT NULL THEN
            DELETE FROM storage.buckets b
            WHERE b.id = 'ticket-images'
              AND NOT EXISTS (
                SELECT 1 FROM storage.objects o WHERE o.bucket_id = 'ticket-images'
              );
          END IF;
        END $$;
        """
    )
