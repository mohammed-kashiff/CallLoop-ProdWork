"""rubrics + audits surrogate PK (multi-rubric, no backfill).

Revision ID: 0003_rubrics_audits
Revises: 0002_api_usage
Create Date: 2026-08-24
"""

from __future__ import annotations

import json
import uuid
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

from backend.audit_store import LEGACY_RUBRIC_NAME, LEGACY_RUBRIC_VERSION, load_v8_definition
from backend.org_ids import DEFAULT_ORG_ID, DEFAULT_RUBRIC_ID

revision: str = "0003_rubrics_audits"
down_revision: Union[str, Sequence[str], None] = "0002_api_usage"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Existing audit rows are dropped, not copied. Runtime is still SQLite;
    # Postgres audits from 0001 are unused. Re-score after the API moves to PG.
    op.execute("DROP TABLE IF EXISTS audits")

    op.execute(
        """
        CREATE TABLE rubrics (
            id UUID PRIMARY KEY,
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            definition JSONB NOT NULL,
            is_active BOOLEAN NOT NULL DEFAULT true,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE UNIQUE INDEX uq_rubrics_org_name_active
            ON rubrics (org_id, name)
            WHERE is_active;
        """
    )

    bind = op.get_bind()
    definition = json.dumps(load_v8_definition(), separators=(",", ":"))
    orgs = bind.execute(text("SELECT id FROM orgs")).fetchall()
    insert_rubric = text(
        """
        INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
        VALUES (
            CAST(:id AS uuid),
            CAST(:org_id AS uuid),
            :name,
            :version,
            CAST(:definition AS jsonb),
            true
        )
        ON CONFLICT (id) DO NOTHING
        """
    )
    for (org_id,) in orgs:
        org_s = str(org_id)
        rubric_id = (
            DEFAULT_RUBRIC_ID if org_s == DEFAULT_ORG_ID else str(uuid.uuid4())
        )
        bind.execute(
            insert_rubric,
            {
                "id": rubric_id,
                "org_id": org_s,
                "name": LEGACY_RUBRIC_NAME,
                "version": LEGACY_RUBRIC_VERSION,
                "definition": definition,
            },
        )

    op.execute(
        """
        CREATE TABLE audits (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            call_id BIGINT NOT NULL REFERENCES calls (id) ON DELETE CASCADE,
            rubric_id UUID NOT NULL REFERENCES rubrics (id) ON DELETE RESTRICT,
            rubric_version INTEGER NOT NULL,
            engine_version TEXT,
            score DOUBLE PRECISION,
            findings JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (call_id, rubric_id, rubric_version)
        );

        CREATE INDEX idx_audits_org_call_created
            ON audits (org_id, call_id, created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX IF EXISTS idx_audits_org_call_created;
        DROP TABLE IF EXISTS audits;
        DROP INDEX IF EXISTS uq_rubrics_org_name_active;
        DROP TABLE IF EXISTS rubrics;

        CREATE TABLE audits (
            call_id BIGINT PRIMARY KEY REFERENCES calls (id) ON DELETE CASCADE,
            org_id UUID NOT NULL REFERENCES orgs (id) ON DELETE RESTRICT,
            audit_json TEXT,
            rubric_hash TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE INDEX idx_audits_org_id ON audits (org_id);
        """
    )
