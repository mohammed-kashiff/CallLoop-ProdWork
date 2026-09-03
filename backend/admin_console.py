"""Platform-admin directory, usage, and feature writes (AC-5).

Every entrypoint assumes require_platform_admin already ran. org_id in the
query/body is the TARGET tenant, not the caller's JWT org. Directory search
uses admin_search_directory() (SECURITY DEFINER) so org_directory stays
ungranted to callproof_app. Usage and flag writes switch GUC to the target
org — never bypass_rls.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    AC-17: deleted calls are NOT filtered out here (opposite of every
    customer-facing list) — they're flagged `deleted` with the deleting
    member's short_id, since that's what support/audit needs. Also reports
    `data_size_bytes` per call: the transcript (raw_json) plus any stored
    audit findings, in Postgres — the two pieces this app actually tracks
    the byte size of. Audio recording size is not included; Storage object
    size isn't tracked in Postgres and would need a separate per-file call
    to Supabase Storage.

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
                SELECT id, filename, job_id, created_at, audio_seconds, uploaded_by,
                       deleted_at, deleted_by,
                       pg_column_size(raw_json) AS raw_json_bytes
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
            audit_size_rows = conn.execute(
                """
                SELECT call_id, SUM(pg_column_size(findings)) AS n
                FROM audits WHERE org_id = %s GROUP BY call_id
                """,
                (oid,),
            ).fetchall()
            member_rows = conn.execute(
                "SELECT user_id, first_name, last_name, short_id FROM org_members WHERE org_id = %s",
                (oid,),
            ).fetchall()
    audited = {int(r["call_id"]) for r in audited_ids or [] if r.get("call_id") is not None}
    requested_by_call = {
        int(r["call_id"]): r.get("requested_by")
        for r in requester_rows or []
        if r.get("call_id") is not None and r.get("requested_by") is not None
    }
    audit_bytes_by_call = {
        int(r["call_id"]): int(r.get("n") or 0)
        for r in audit_size_rows or []
        if r.get("call_id") is not None
    }
    names = {str(r["user_id"]): _member_name(r) for r in member_rows or [] if r.get("user_id")}
    short_ids = {
        str(r["user_id"]): r.get("short_id") for r in member_rows or [] if r.get("user_id")
    }
    total_calls = int((total_row or {}).get("n") or 0)
    calls = []
    total_data_size_bytes = 0
    for row in call_rows or []:
        call_id = int(row["id"])
        uploaded_by = row.get("uploaded_by")
        requested_by = requested_by_call.get(call_id)
        deleted_by = row.get("deleted_by")
        data_size_bytes = int(row.get("raw_json_bytes") or 0) + audit_bytes_by_call.get(call_id, 0)
        total_data_size_bytes += data_size_bytes
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
                "deleted": row.get("deleted_at") is not None,
                "deleted_at": _json_value(row.get("deleted_at")),
                "deleted_by_short_id": short_ids.get(str(deleted_by)) if deleted_by else None,
                "data_size_bytes": data_size_bytes,
            }
        )
    return {
        "org_id": oid,
        "total_calls": total_calls,
        "audited_count": int((audited_row or {}).get("n") or 0),
        "calls": calls,
        "calls_truncated": total_calls > len(calls),
        "total_data_size_bytes": total_data_size_bytes,
    }


_ACTIVITY_LIMIT_MAX = 500
_ACTIVITY_LIMIT_DEFAULT = 200
_ACTIVITY_WINDOW_DAYS = 366


def _parse_short_id(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s or not s.isdigit():
        return None
    n = int(s)
    if n < 1 or n > 2_147_483_647:
        return None
    return n


def resolve_activity_org(org_id: str | None, short_id: str | None) -> str:
    """Platform-admin target org. UUID wins; otherwise short_id on org_members."""
    oid = parse_org_id(org_id)
    if oid:
        return oid
    sid = _parse_short_id(short_id) or _parse_short_id(org_id)
    if sid is None:
        raise HTTPException(status_code=400, detail="org_id or short_id is required.")
    with db.connection() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE short_id = %s",
            (sid,),
        ).fetchone()
    oid = parse_org_id((row or {}).get("org_id"))
    if not oid:
        raise HTTPException(status_code=404, detail="No org matches that short_id.")
    return oid


def _parse_activity_bound(raw: str | None, *, end: bool) -> datetime:
    s = (raw or "").strip()
    if not s:
        raise HTTPException(status_code=400, detail="since and until are required.")
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            day = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return day + timedelta(days=1) if end else day
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=400, detail="since and until must be dates.")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def activity(
    org_id: str | None = None,
    short_id: str | None = None,
    *,
    since: str | None,
    until: str | None,
    limit: int | None = None,
) -> dict:
    """Structured org activity from calls/audits/org_features_history — not app logs."""
    oid = resolve_activity_org(org_id, short_id)
    start = _parse_activity_bound(since, end=False)
    stop = _parse_activity_bound(until, end=True)
    if stop <= start:
        raise HTTPException(status_code=400, detail="until must be after since.")
    if (stop - start) > timedelta(days=_ACTIVITY_WINDOW_DAYS):
        raise HTTPException(status_code=400, detail="Date range is too large.")
    cap = max(1, min(int(limit or _ACTIVITY_LIMIT_DEFAULT), _ACTIVITY_LIMIT_MAX))
    with org_scope(oid):
        with db.connection() as conn:
            uploads = conn.execute(
                """
                SELECT id, filename, created_at, uploaded_by
                FROM calls
                WHERE org_id = %s AND created_at >= %s AND created_at < %s
                ORDER BY created_at DESC
                """,
                (oid, start, stop),
            ).fetchall()
            audits = conn.execute(
                """
                SELECT id, call_id, created_at, requested_by
                FROM audits
                WHERE org_id = %s AND created_at >= %s AND created_at < %s
                ORDER BY created_at DESC
                """,
                (oid, start, stop),
            ).fetchall()
            flags = conn.execute(
                """
                SELECT feature_key, enabled, changed_by, changed_at
                FROM org_features_history
                WHERE org_id = %s AND changed_at >= %s AND changed_at < %s
                ORDER BY changed_at DESC
                """,
                (oid, start, stop),
            ).fetchall()
            deletes = conn.execute(
                """
                SELECT id, filename, deleted_at, deleted_by
                FROM calls
                WHERE org_id = %s AND deleted_at >= %s AND deleted_at < %s
                ORDER BY deleted_at DESC
                """,
                (oid, start, stop),
            ).fetchall()
            member_rows = conn.execute(
                "SELECT user_id, first_name, last_name FROM org_members WHERE org_id = %s",
                (oid,),
            ).fetchall()
    names = {
        str(r["user_id"]): _member_name(r)
        for r in member_rows or []
        if r.get("user_id")
    }
    events: list[dict] = []
    for row in uploads or []:
        actor_id = row.get("uploaded_by")
        events.append(
            {
                "at": _json_value(row.get("created_at")),
                "kind": "upload",
                "actor": names.get(str(actor_id)) if actor_id else None,
                "call_id": int(row["id"]) if row.get("id") is not None else None,
                "filename": row.get("filename"),
                "feature_key": None,
                "enabled": None,
            }
        )
    for row in audits or []:
        actor_id = row.get("requested_by")
        events.append(
            {
                "at": _json_value(row.get("created_at")),
                "kind": "audit",
                "actor": names.get(str(actor_id)) if actor_id else None,
                "call_id": int(row["call_id"]) if row.get("call_id") is not None else None,
                "filename": None,
                "feature_key": None,
                "enabled": None,
            }
        )
    for row in flags or []:
        events.append(
            {
                "at": _json_value(row.get("changed_at")),
                "kind": "flag_change",
                "actor": str(row.get("changed_by") or "") or None,
                "call_id": None,
                "filename": None,
                "feature_key": row.get("feature_key"),
                "enabled": bool(row.get("enabled")) if row.get("enabled") is not None else None,
            }
        )
    for row in deletes or []:
        actor_id = row.get("deleted_by")
        events.append(
            {
                "at": _json_value(row.get("deleted_at")),
                "kind": "delete",
                "actor": names.get(str(actor_id)) if actor_id else None,
                "call_id": int(row["id"]) if row.get("id") is not None else None,
                "filename": row.get("filename"),
                "feature_key": None,
                "enabled": None,
            }
        )
    events.sort(key=lambda e: (e.get("at") or ""), reverse=True)
    truncated = len(events) > cap
    return {
        "org_id": oid,
        "since": start.isoformat(),
        "until": stop.isoformat(),
        "events": events[:cap],
        "truncated": truncated,
    }
