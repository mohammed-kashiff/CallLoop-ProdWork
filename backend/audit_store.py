"""Audit persistence keyed for multi-rubric scoring.

Why this shape exists
---------------------
``audits`` used to be ``PRIMARY KEY (call_id)``, so a call could hold exactly
one scorecard. Scoring a second rubric overwrote the first.

Do **not** use ``PRIMARY KEY (call_id, rubric_id)``. That still blocks
re-evaluation after a transcript correction or a rubric revision.

Use a surrogate ``id`` plus ``UNIQUE (call_id, rubric_id, rubric_version)``.
That gives multi-rubric scoring, history across versions, and idempotent
retries. If multiple runs of the same version are needed later, add
``evaluation_run_id`` into the unique constraint — additive, no key change.

Read contract
-------------
``SELECT * FROM audits WHERE call_id = ?`` now returns several rows. Every
site must choose one of:

* latest-per-rubric (``MAX(rubric_version)`` for a given ``rubric_id``)
* full history
* a specific audit ``id``

Never take an arbitrary row. Never mutate a rubric in place once audits
reference it — bump ``version`` and insert a new rubric row.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .org_ids import DEFAULT_ORG_ID, DEFAULT_RUBRIC_ID
from .paths import RUBRIC_PATH

LEGACY_RUBRIC_NAME = "Default (legacy v8)"
LEGACY_RUBRIC_VERSION = 1


def load_v8_definition() -> dict[str, Any]:
    """Existing qa_v8 / rubric.json shape, stored unchanged in rubrics.definition."""
    with open(RUBRIC_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("rubric.json must be a JSON object")
    return data


def parse_scorecard(row: sqlite3.Row | None) -> tuple[dict | None, str | None]:
    """Decode the stored scorecard blob from ``findings`` plus ``engine_version``."""
    if row is None:
        return None, None
    engine = row["engine_version"]
    try:
        payload = json.loads(row["findings"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None, engine
    if not isinstance(payload, dict):
        return None, engine
    return payload, engine


def _audits_is_legacy(conn: sqlite3.Connection) -> bool:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(audits)").fetchall()}
    if not cols:
        return False
    return "rubric_id" not in cols or "id" not in cols


def ensure_sqlite_schema(conn: sqlite3.Connection) -> None:
    """Local SQLite mirror of Alembic 0003. Drops the old PK(call_id) audits table.

    Existing scorecards are not backfilled — that is intentional. Recreate
    scores by re-running audit after this boots against an old ``callproof.db``.
    """
    if _audits_is_legacy(conn):
        conn.execute("DROP TABLE IF EXISTS audits")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rubrics (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            name TEXT NOT NULL,
            version INTEGER NOT NULL,
            definition TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_rubrics_org_name_active
        ON rubrics (org_id, name)
        WHERE is_active = 1
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audits (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL,
            call_id INTEGER NOT NULL,
            rubric_id TEXT NOT NULL,
            rubric_version INTEGER NOT NULL,
            engine_version TEXT,
            score REAL,
            findings TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (call_id, rubric_id, rubric_version)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_audits_org_call_created
        ON audits (org_id, call_id, created_at DESC)
        """
    )
    seed_legacy_rubric(conn, org_id=DEFAULT_ORG_ID, rubric_id=DEFAULT_RUBRIC_ID)
    conn.commit()


def seed_legacy_rubric(
    conn: sqlite3.Connection,
    *,
    org_id: str,
    rubric_id: str,
) -> None:
    """Insert the hardcoded v8 definition once. Never UPDATE definition in place."""
    row = conn.execute("SELECT id FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
    if row:
        return
    definition = json.dumps(load_v8_definition(), separators=(",", ":"))
    conn.execute(
        """
        INSERT INTO rubrics (
            id, org_id, name, version, definition, is_active, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, 1, datetime('now'), datetime('now'))
        ON CONFLICT (id) DO NOTHING
        """,
        (rubric_id, org_id, LEGACY_RUBRIC_NAME, LEGACY_RUBRIC_VERSION, definition),
    )


def fetch_latest_for_rubric(
    conn: sqlite3.Connection,
    *,
    call_id: int,
    rubric_id: str,
    org_id: str = DEFAULT_ORG_ID,
) -> sqlite3.Row | None:
    """latest-per-rubric: highest rubric_version for this call + rubric + org."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = ? AND call_id = ? AND rubric_id = ?
        ORDER BY rubric_version DESC, created_at DESC
        LIMIT 1
        """,
        (org_id, call_id, rubric_id),
    ).fetchone()


def fetch_history(
    conn: sqlite3.Connection,
    *,
    call_id: int,
    org_id: str = DEFAULT_ORG_ID,
) -> list[sqlite3.Row]:
    """full history: every audit row for this call in this org."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = ? AND call_id = ?
        ORDER BY created_at DESC
        """,
        (org_id, call_id),
    ).fetchall()


def fetch_by_id(
    conn: sqlite3.Connection,
    *,
    audit_id: str,
    org_id: str = DEFAULT_ORG_ID,
) -> sqlite3.Row | None:
    """specific audit id, still scoped to org."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = ? AND id = ?
        """,
        (org_id, audit_id),
    ).fetchone()


def latest_default_join_sql(*, inner: bool = False) -> str:
    """JOIN fragment: latest-per-rubric for the default legacy rubric.

    Bind order: org_id, rubric_id, org_id, rubric_id.
    """
    kind = "INNER JOIN" if inner else "LEFT JOIN"
    return f"""
            {kind} audits a
              ON a.call_id = c.id
             AND a.org_id = ?
             AND a.rubric_id = ?
             AND a.rubric_version = (
                SELECT MAX(a2.rubric_version)
                  FROM audits a2
                 WHERE a2.call_id = c.id
                   AND a2.org_id = ?
                   AND a2.rubric_id = ?
             )
    """


def latest_default_join_params(org_id: str = DEFAULT_ORG_ID) -> tuple[str, str, str, str]:
    return (org_id, DEFAULT_RUBRIC_ID, org_id, DEFAULT_RUBRIC_ID)


def upsert_audit(
    conn: sqlite3.Connection,
    *,
    call_id: int,
    findings: dict,
    engine_version: str,
    org_id: str = DEFAULT_ORG_ID,
    rubric_id: str = DEFAULT_RUBRIC_ID,
    rubric_version: int = LEGACY_RUBRIC_VERSION,
) -> str:
    """INSERT ... ON CONFLICT (call_id, rubric_id, rubric_version) DO UPDATE.

    Same rubric version retries one row. A different rubric_id persists both.
    """
    audit_id = str(uuid.uuid4())
    score = findings.get("score") if isinstance(findings, dict) else None
    conn.execute(
        """
        INSERT INTO audits (
            id, org_id, call_id, rubric_id, rubric_version,
            engine_version, score, findings, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (call_id, rubric_id, rubric_version) DO UPDATE SET
            engine_version = excluded.engine_version,
            score = excluded.score,
            findings = excluded.findings
        """,
        (
            audit_id,
            org_id,
            call_id,
            rubric_id,
            rubric_version,
            engine_version,
            score,
            json.dumps(findings),
        ),
    )
    return audit_id
