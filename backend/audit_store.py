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

import copy
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


def _iter_dimensions(definition: dict):
    for bucket in ("technical_skills", "soft_skills"):
        for dim in ((definition.get(bucket) or {}).get("dimensions") or []):
            yield dim


def apply_dimension_weights(definition: dict, weights: Mapping[str, int]) -> dict[str, Any]:
    """Copy `definition` and set each known dimension's weight. Never mutates in place."""
    out = copy.deepcopy(definition)
    seen: set[str] = set()
    for dim in _iter_dimensions(out):
        did = dim.get("id")
        if did in weights:
            dim["weight"] = weights[did]
            seen.add(did)
    if seen != set(weights):
        missing = ", ".join(sorted(set(weights) - seen)) or "dimension"
        raise ValueError(f"rubric is missing scoring dimension: {missing}")
    return out


def insert_weighted_version(
    conn,
    *,
    org_id: str,
    weights: Mapping[str, int],
) -> dict[str, Any]:
    """Insert a new active rubric version; deactivate every prior active row.

    Reuses the current active row's `name` (or LEGACY_RUBRIC_NAME when the org
    has no row yet). Inventing a new name would dodge uq_rubrics_org_name_active
    and leave two active rows for one org. Never UPDATEs definition in place.
    Caller must hold a transaction that commits both writes together.
    """
    active = conn.execute(
        """
        SELECT id, name, version, definition
        FROM rubrics
        WHERE org_id = %s AND is_active
        ORDER BY version DESC
        LIMIT 1
        FOR UPDATE
        """,
        (org_id,),
    ).fetchone()
    if active:
        name = str(active["name"] or "") or LEGACY_RUBRIC_NAME
        base = decode_findings(active.get("definition"))
        if not isinstance(base, dict):
            base = load_v8_definition()
    else:
        name = LEGACY_RUBRIC_NAME
        base = load_v8_definition()
    previous_weights = {dim.get("id"): dim.get("weight") for dim in _iter_dimensions(base)}
    definition = apply_dimension_weights(base, weights)
    max_row = conn.execute(
        """
        SELECT COALESCE(MAX(version), 0) AS v
        FROM rubrics
        WHERE org_id = %s AND name = %s
        """,
        (org_id, name),
    ).fetchone()
    version = int((max_row or {}).get("v") or 0) + 1
    conn.execute(
        """
        UPDATE rubrics
        SET is_active = false, updated_at = now()
        WHERE org_id = %s AND is_active
        """,
        (org_id,),
    )
    rubric_id = str(uuid.uuid4())
    inserted = conn.execute(
        """
        INSERT INTO rubrics (
            id, org_id, name, version, definition, is_active, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, true, now(), now())
        RETURNING id, name, version, updated_at
        """,
        (rubric_id, org_id, name, version, Json(definition)),
    ).fetchone()
    row = inserted or {
        "id": rubric_id,
        "name": name,
        "version": version,
        "updated_at": None,
    }
    return {
        "rubric_id": str(row["id"]),
        "name": str(row["name"]),
        "version": int(row["version"]),
        "updated_at": row.get("updated_at"),
        "weights": dict(weights),
        "previous_weights": previous_weights,
        "definition": definition,
    }


def insert_custom_definition(conn, *, org_id: str, definition: Mapping[str, Any]) -> dict[str, Any]:
    """Insert a new active rubric version with a caller-built definition.

    Self-serve rubric builder: the dimension SET itself can change (not just
    weights on the existing 4) — the caller already composed `definition`
    from a team's own built-in/custom picks. Same versioning/name-reuse/
    one-transaction discipline as insert_weighted_version (CR-13), kept as a
    separate function rather than refactored into it so Command Center's
    admin-only reweighting tool stays untouched.
    """
    active = conn.execute(
        """
        SELECT id, name, version
        FROM rubrics
        WHERE org_id = %s AND is_active
        ORDER BY version DESC
        LIMIT 1
        FOR UPDATE
        """,
        (org_id,),
    ).fetchone()
    name = (str(active["name"] or "") if active else "") or LEGACY_RUBRIC_NAME
    max_row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM rubrics WHERE org_id = %s AND name = %s",
        (org_id, name),
    ).fetchone()
    version = int((max_row or {}).get("v") or 0) + 1
    conn.execute(
        "UPDATE rubrics SET is_active = false, updated_at = now() WHERE org_id = %s AND is_active",
        (org_id,),
    )
    rubric_id = str(uuid.uuid4())
    inserted = conn.execute(
        """
        INSERT INTO rubrics (
            id, org_id, name, version, definition, is_active, created_at, updated_at
        )
        VALUES (%s, %s, %s, %s, %s, true, now(), now())
        RETURNING id, name, version, updated_at
        """,
        (rubric_id, org_id, name, version, Json(dict(definition))),
    ).fetchone()
    row = inserted or {"id": rubric_id, "name": name, "version": version, "updated_at": None}
    return {
        "rubric_id": str(row["id"]),
        "name": str(row["name"]),
        "version": int(row["version"]),
        "updated_at": row.get("updated_at"),
        "definition": definition,
    }


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
