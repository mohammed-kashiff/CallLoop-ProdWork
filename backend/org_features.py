"""Per-org UI feature flags (AC-4).

A missing org+key row means the flag is on for trial-run UI switches. That
default is what keeps existing orgs unchanged on rollout. /api/me overlays DB
rows onto FEATURE_KEYS; extra keys in the table are returned too so new flags
do not need a migration.

Exception: use_selfhosted_transcription is off when unset, so deploy does not
silently move orgs off PyAI Hear.

org_id is the JWT tenant only. Do not read it from the request body here.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import db
from .org_ids import parse_org_id

log = logging.getLogger("callproof.org_features")

# Trial-run dashboard switches. Insert other keys without a schema change.
FEATURE_KEYS = (
    "show_usage_bar",
    "show_neighbourhood_nav",
    "show_growth_tools_nav",
    "show_powered_by_pyai",
    "show_billed_usage_panel",
    "use_selfhosted_transcription",
)

# Missing key → on, except this engine switch. Unset must stay on PyAI.
DEFAULT_OFF_KEYS = frozenset({"use_selfhosted_transcription"})


def default_features() -> dict[str, bool]:
    return {key: key not in DEFAULT_OFF_KEYS for key in FEATURE_KEYS}


def features_for_org(org_id: str) -> dict[str, bool]:
    """Flags for this org. Missing trial-run keys are on; self-hosted ASR is off."""
    flags = default_features()
    oid = parse_org_id(org_id)
    if not oid:
        return flags
    try:
        with db.connection() as conn:
            db.apply_tenant_gucs(conn, org_id=oid)
            rows = conn.execute(
                """
                SELECT feature_key, enabled
                FROM org_features
                WHERE org_id = %s
                """,
                (oid,),
            ).fetchall()
    except Exception as e:  # noqa: BLE001
        log.debug("org_features lookup skipped: %s", e)
        return flags
    for row in rows or []:
        key = str(row.get("feature_key") or "").strip()
        if not key:
            continue
        flags[key] = bool(row.get("enabled"))
    return flags


def set_feature(
    org_id: str, feature_key: str, enabled: bool, *, changed_by: str,
) -> dict[str, bool]:
    """Upsert one flag and append a history row. Caller must already be a platform admin."""
    oid = parse_org_id(org_id)
    key = (feature_key or "").strip()
    actor = (changed_by or "").strip().lower()
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    if key not in FEATURE_KEYS:
        raise HTTPException(status_code=400, detail="Unknown feature_key.")
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")
    on = bool(enabled)
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        conn.execute(
            """
            INSERT INTO org_features (org_id, feature_key, enabled, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (org_id, feature_key) DO UPDATE
            SET enabled = EXCLUDED.enabled, updated_at = now()
            """,
            (oid, key, on),
        )
        conn.execute(
            """
            INSERT INTO org_features_history (
                org_id, feature_key, enabled, changed_by
            )
            VALUES (%s, %s, %s, %s)
            """,
            (oid, key, on, actor),
        )
    applog.event(
        log, "org_feature_changed",
        org_id=oid,
        feature_key=key,
        enabled=on,
        changed_by=actor,
    )
    return features_for_org(oid)


def feature_history(org_id: str, feature_key: str) -> list[dict]:
    """Chronological flag changes for this org+key. Empty if the org id is invalid."""
    oid = parse_org_id(org_id)
    key = (feature_key or "").strip()
    if not oid or not key:
        return []
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        rows = conn.execute(
            """
            SELECT org_id, feature_key, enabled, changed_by, changed_at
            FROM org_features_history
            WHERE org_id = %s AND feature_key = %s
            ORDER BY changed_at ASC, id ASC
            """,
            (oid, key),
        ).fetchall()
    out: list[dict] = []
    for row in rows or []:
        out.append(
            {
                "org_id": str(row.get("org_id") or ""),
                "feature_key": str(row.get("feature_key") or ""),
                "enabled": bool(row.get("enabled")),
                "changed_by": str(row.get("changed_by") or ""),
                "changed_at": row.get("changed_at"),
            }
        )
    return out
