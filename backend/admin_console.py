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


_DETAIL_LIMIT_MAX = 500
_DETAIL_LIMIT_DEFAULT = 200


def _call_mode(job_id: str | None) -> str:
    """selfhosted_* job ids are ours (Modal); everything else is a PyAI job id."""
    return "selfhosted" if (job_id or "").startswith("selfhosted_") else "pyai"


def _member_name(row) -> str | None:
    n = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p).strip()
    return n or None


def call_detail(org_id: str | None, limit: int | None = None) -> dict:
    """Org call volume, audit coverage, and which engine ran each call (AC-13).

    Also surfaces who uploaded each call and who last requested its audit
    (AC-14) — both resolved to a display name via org_members, since email
    lives outside this app's normal role (Supabase auth, not org_members).

    Counts are true totals (COUNT(*)/COUNT(DISTINCT)), not capped by the
    per-call listing's limit, which stays reasonable for a single JSON
    response — callers needing the full list can page by created_at.
    """
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    cap = max(1, min(int(limit or _DETAIL_LIMIT_DEFAULT), _DETAIL_LIMIT_MAX))
    with org_scope(oid):
        with db.connection() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE org_id = %s", (oid,),
            ).fetchone()
            audited_row = conn.execute(
                "SELECT COUNT(DISTINCT call_id) AS n FROM audits WHERE org_id = %s",
                (oid,),
            ).fetchone()
            call_rows = conn.execute(
                """
                SELECT id, filename, job_id, created_at, audio_seconds, uploaded_by
                FROM calls
                WHERE org_id = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (oid, cap),
            ).fetchall()
            audited_ids = conn.execute(
                "SELECT DISTINCT call_id FROM audits WHERE org_id = %s", (oid,),
            ).fetchall()
            requester_rows = conn.execute(
                """
                SELECT DISTINCT ON (call_id) call_id, requested_by
                FROM audits
                WHERE org_id = %s
                ORDER BY call_id, created_at DESC
                """,
                (oid,),
            ).fetchall()
            member_rows = conn.execute(
                "SELECT user_id, first_name, last_name FROM org_members WHERE org_id = %s",
                (oid,),
            ).fetchall()
    audited = {int(r["call_id"]) for r in audited_ids or [] if r.get("call_id") is not None}
    requested_by_call = {
        int(r["call_id"]): r.get("requested_by")
        for r in requester_rows or []
        if r.get("call_id") is not None and r.get("requested_by") is not None
    }
    names = {str(r["user_id"]): _member_name(r) for r in member_rows or [] if r.get("user_id")}
    total_calls = int((total_row or {}).get("n") or 0)
    calls = []
    for row in call_rows or []:
        call_id = int(row["id"])
        uploaded_by = row.get("uploaded_by")
        requested_by = requested_by_call.get(call_id)
        calls.append(
            {
                "call_id": call_id,
                "filename": row.get("filename"),
                "created_at": _json_value(row.get("created_at")),
                "audio_seconds": row.get("audio_seconds"),
                "mode": _call_mode(row.get("job_id")),
                "audited": call_id in audited,
                "uploaded_by": names.get(str(uploaded_by)) if uploaded_by else None,
                "requested_by": names.get(str(requested_by)) if requested_by else None,
            }
        )
    return {
        "org_id": oid,
        "total_calls": total_calls,
        "audited_count": int((audited_row or {}).get("n") or 0),
        "calls": calls,
        "calls_truncated": total_calls > len(calls),
    }
