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
``SELECT * FROM audits WHERE call_id = %s`` now returns several rows. Every
site must choose one of:

* latest-per-rubric (``MAX(rubric_version)`` for a given ``rubric_id``)
* full history
* a specific audit ``id``

Never take an arbitrary row. Never mutate a rubric in place once audits
reference it — bump ``version`` and insert a new rubric row.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from typing import Any

from psycopg.types.json import Json

from .org_ids import DEFAULT_ORG_ID, DEFAULT_RUBRIC_ID
from .paths import RUBRIC_PATH

LEGACY_RUBRIC_NAME = "Default (legacy v8)"
LEGACY_RUBRIC_VERSION = 1

Row = Mapping[str, Any]


def load_v8_definition() -> dict[str, Any]:
    """Existing qa_v8 / rubric.json shape, stored unchanged in rubrics.definition."""
    with open(RUBRIC_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("rubric.json must be a JSON object")
    return data


def fetch_active_rubric(conn, *, org_id: str) -> tuple[str, int, dict[str, Any]]:
    """(rubric_id, version, definition) for the org's active row (FR1, CR-11).

    Resolved once by the caller and reused for both the cache lookup and the
    eventual write — never re-queried mid-request (PRD edge case: a save
    racing a scoring run must not partially apply).

    No active row (including "no rubrics row at all") falls back to the
    legacy identity every org shared before this feature existed —
    DEFAULT_RUBRIC_ID / LEGACY_RUBRIC_VERSION / the rubric.json file. No
    proactive seeding — an org with nothing in rubrics yet stays on this
    fallback until an admin saves a custom version (CR-13).
    """
    row = conn.execute(
        "SELECT id, version, definition FROM rubrics WHERE org_id = %s AND is_active LIMIT 1",
        (org_id,),
    ).fetchone()
    definition = decode_findings((row or {}).get("definition"))
    if row and isinstance(definition, dict):
        return str(row["id"]), int(row["version"]), definition
    return DEFAULT_RUBRIC_ID, LEGACY_RUBRIC_VERSION, load_v8_definition()


def decode_findings(raw: Any) -> dict | None:
    """JSONB may already be a dict; TEXT-era rows are a JSON string."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def parse_scorecard(row: Row | None) -> tuple[dict | None, str | None]:
    """Decode the stored scorecard blob from ``findings`` plus ``engine_version``."""
    if row is None:
        return None, None
    return decode_findings(row["findings"]), row["engine_version"]


def seed_legacy_rubric(
    conn,
    *,
    org_id: str,
    rubric_id: str,
) -> None:
    """Insert the hardcoded v8 definition once. Never UPDATE definition in place."""
    row = conn.execute(
        "SELECT id FROM rubrics WHERE id = %s",
        (rubric_id,),
    ).fetchone()
    if row:
        return
    conn.execute(
        """
        INSERT INTO rubrics (
            id, org_id, name, version, definition, is_active, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, true, now(), now())
        ON CONFLICT (id) DO NOTHING
        """,
        (
            rubric_id,
            org_id,
            LEGACY_RUBRIC_NAME,
            LEGACY_RUBRIC_VERSION,
            Json(load_v8_definition()),
        ),
    )


def fetch_latest_for_rubric(
    conn,
    *,
    call_id: int,
    rubric_id: str,
    org_id: str = DEFAULT_ORG_ID,
) -> Row | None:
    """latest-per-rubric: highest rubric_version for this call + rubric + org."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = %s AND call_id = %s AND rubric_id = %s
        ORDER BY rubric_version DESC, created_at DESC
        LIMIT 1
        """,
        (org_id, call_id, rubric_id),
    ).fetchone()


def fetch_latest(conn, *, call_id: int, org_id: str = DEFAULT_ORG_ID) -> Row | None:
    """Most recent audit for this call, regardless of which rubric produced
    it (CR-12). A call's cached result is never invalidated just because the
    org's active rubric changed since — the PRD requires a weight change to
    apply going forward only, so the cache-hit check must not filter by the
    org's *current* rubric_id, or an org-wide rubric edit would silently
    look like a cache miss for every already-scored call in that org and
    quietly re-score them under the new weights on next view."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = %s AND call_id = %s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (org_id, call_id),
    ).fetchone()


def fetch_history(
    conn,
    *,
    call_id: int,
    org_id: str = DEFAULT_ORG_ID,
) -> list[Row]:
    """full history: every audit row for this call in this org."""
    return list(
        conn.execute(
            """
            SELECT *
            FROM audits
            WHERE org_id = %s AND call_id = %s
            ORDER BY created_at DESC
            """,
            (org_id, call_id),
        ).fetchall()
    )


def fetch_by_id(
    conn,
    *,
    audit_id: str,
    org_id: str = DEFAULT_ORG_ID,
) -> Row | None:
    """specific audit id, still scoped to org."""
    return conn.execute(
        """
        SELECT *
        FROM audits
        WHERE org_id = %s AND id = %s
        """,
        (org_id, audit_id),
    ).fetchone()


def latest_default_join_sql(*, inner: bool = False) -> str:
    """JOIN fragment: latest-per-rubric for the default legacy rubric.

    Bind order: org_id, rubric_id, org_id, rubric_id.
    INNER vs LEFT is a fixed keyword, not user input.
    """
    kind = "INNER JOIN" if inner else "LEFT JOIN"
    return f"""
            {kind} audits a
              ON a.call_id = c.id
             AND a.org_id = %s
             AND a.rubric_id = %s
             AND a.rubric_version = (
                SELECT MAX(a2.rubric_version)
                  FROM audits a2
                 WHERE a2.call_id = c.id
                   AND a2.org_id = %s
                   AND a2.rubric_id = %s
             )
    """


def latest_default_join_params(org_id: str = DEFAULT_ORG_ID) -> tuple[str, str, str, str]:
    return (org_id, DEFAULT_RUBRIC_ID, org_id, DEFAULT_RUBRIC_ID)


def upsert_audit(
    conn,
    *,
    call_id: int,
    findings: dict,
    engine_version: str,
    org_id: str = DEFAULT_ORG_ID,
    rubric_id: str = DEFAULT_RUBRIC_ID,
    rubric_version: int = LEGACY_RUBRIC_VERSION,
    requested_by: str | None = None,
) -> str:
    """INSERT ... ON CONFLICT (call_id, rubric_id, rubric_version) DO UPDATE.

    Same rubric version retries one row. A different rubric_id persists both.
    requested_by only overwrites on conflict when a real actor is passed (an
    incidental cache-read recompute with no request context must not erase a
    previously recorded attribution).
    """
    audit_id = str(uuid.uuid4())
    score = findings.get("score") if isinstance(findings, dict) else None
    if rubric_id == DEFAULT_RUBRIC_ID:
        seed_legacy_rubric(conn, org_id=org_id, rubric_id=DEFAULT_RUBRIC_ID)
    conn.execute(
        """
        INSERT INTO audits (
            id, org_id, call_id, rubric_id, rubric_version,
            engine_version, score, findings, requested_by, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (call_id, rubric_id, rubric_version) DO UPDATE SET
            engine_version = excluded.engine_version,
            score = excluded.score,
            findings = excluded.findings,
            requested_by = COALESCE(excluded.requested_by, audits.requested_by)
        """,
        (
            audit_id,
            org_id,
            call_id,
            rubric_id,
            rubric_version,
            engine_version,
            score,
            Json(findings),
            requested_by,
        ),
    )
    return audit_id
