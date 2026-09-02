"""Platform-admin directory, usage, and feature writes (AC-5).

Every entrypoint assumes require_platform_admin already ran. org_id in the
query/body is the TARGET tenant, not the caller's JWT org. Directory search
uses admin_search_directory() (SECURITY DEFINER) so org_directory stays
ungranted to callproof_app. Usage and flag writes switch GUC to the target
org — never bypass_rls.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import HTTPException

from . import cost_estimate
from . import db
from . import org_features
from . import pyai_usage
from .org_ids import org_scope, parse_org_id

_ALL_TIME = "1970-01-01T00:00:00Z"
_Q_MAX = 80


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _json_row(row) -> dict:
    return {str(k): _json_value(v) for k, v in dict(row).items()}


def search_directory(q: str | None) -> dict:
    needle = (q or "").strip()[:_Q_MAX]
    try:
        with db.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM public.admin_search_directory(%s)",
                (needle,),
            ).fetchall()
    except Exception:
        return {"rows": []}
    return {"rows": [_json_row(r) for r in rows or []]}


def usage_for_org(org_id: str | None) -> dict:
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        summary = pyai_usage.usage_summary(since_iso=_ALL_TIME, org_id=oid)
        flags = org_features.features_for_org(oid)
    return {
        "org_id": oid,
        "usage": summary,
        "cost": cost_estimate.estimate_usage_cost(summary),
        "features": flags,
    }


def set_feature(
    org_id: str | None, feature_key: str, enabled: bool, *, changed_by: str,
) -> dict:
    flags = org_features.set_feature(
        org_id or "", feature_key, enabled, changed_by=changed_by,
    )
    return {"org_id": parse_org_id(org_id), "features": flags}
