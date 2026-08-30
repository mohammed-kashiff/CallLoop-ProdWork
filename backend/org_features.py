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


def set_feature(org_id: str, feature_key: str, enabled: bool) -> dict[str, bool]:
    """Upsert one flag for the target org. Caller must already be a platform admin."""
    oid = parse_org_id(org_id)
    key = (feature_key or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    if key not in FEATURE_KEYS:
        raise HTTPException(status_code=400, detail="Unknown feature_key.")
    with db.connection() as conn:
        db.apply_tenant_gucs(conn, org_id=oid)
        conn.execute(
            """
            INSERT INTO org_features (org_id, feature_key, enabled, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (org_id, feature_key) DO UPDATE
            SET enabled = EXCLUDED.enabled, updated_at = now()
            """,
            (oid, key, bool(enabled)),
        )
    return features_for_org(oid)
