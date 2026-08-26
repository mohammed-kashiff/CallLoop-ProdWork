"""Private call-audio Storage bucket (no-op on vanilla Postgres).

Revision ID: 0008_storage_audio_bucket
Revises: 0007_org_members_no_rls
Create Date: 2026-08-27

CI Postgres has no storage schema. On Supabase, insert a private bucket
named call-audio. Runtime also ensure_bucket() via the Storage API so a
missing row still works. Do not add public/authenticated read policies —
signed URLs are minted by the API with the service role after JWT check.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0008_storage_audio_bucket"
down_revision: Union[str, Sequence[str], None] = "0007_org_members_no_rls"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('storage.buckets') IS NOT NULL THEN
            INSERT INTO storage.buckets (id, name, public)
            VALUES ('call-audio', 'call-audio', false)
            ON CONFLICT (id) DO UPDATE SET public = false;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    # Leave objects in place. Only drop the bucket row when empty.
    op.execute(
        """
        DO $$
        BEGIN
          IF to_regclass('storage.buckets') IS NOT NULL
             AND to_regclass('storage.objects') IS NOT NULL THEN
            DELETE FROM storage.buckets b
            WHERE b.id = 'call-audio'
              AND NOT EXISTS (
                SELECT 1 FROM storage.objects o WHERE o.bucket_id = 'call-audio'
              );
          END IF;
        END $$;
        """
    )
