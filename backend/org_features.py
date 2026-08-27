"""Per-org UI feature flags (AC-4).

A missing org+key row means the flag is on. That default is what keeps
existing orgs unchanged on rollout. /api/me overlays DB rows onto FEATURE_KEYS;
extra keys in the table are returned too so new flags do not need a migration.

org_id is the JWT tenant only. Do not read it from the request body here.
"""

from __future__ import annotations

import logging

from . import db
from .org_ids import parse_org_id

log = logging.getLogger("callproof.org_features")

# Trial-run dashboard switches. Insert other keys without a schema change.
FEATURE_KEYS = (
    "usage_bar",
    "secondary_nav",
    "powered_by_badge",
    "billed_usage_panel",
)


def default_features() -> dict[str, bool]:
    return {key: True for key in FEATURE_KEYS}


def features_for_org(org_id: str) -> dict[str, bool]:
    """Flags for this org. Missing keys are enabled. Read-only."""
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
