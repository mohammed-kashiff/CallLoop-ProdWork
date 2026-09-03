"""Platform-admin directory, usage, and feature writes (AC-5).

Every entrypoint assumes require_platform_admin already ran. org_id in the
query/body is the TARGET tenant, not the caller's JWT org. Directory search
uses admin_search_directory() (SECURITY DEFINER) so org_directory stays
ungranted to callproof_app. Usage and flag writes switch GUC to the target
org — never bypass_rls.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import HTTPException

from . import applog
from . import audit_store
from . import cost_estimate
from . import db
from . import org_features
from . import pyai_usage
from . import qa_v8
from .org_ids import org_scope, parse_org_id

_ALL_TIME = "1970-01-01T00:00:00Z"
_Q_MAX = 80
_WEIGHT_SUM = 100

log = logging.getLogger("callproof.admin")


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


_CALL_LOGS_LIMIT_MAX = 5000
_CALL_LOGS_LIMIT_DEFAULT = 500


def _resolve_call_log_scope(query: str) -> tuple[str, str | None, dict]:
    """Resolve an email, org id, or short id into (org_id, uploaded_by_filter, matched).

    A bare org id always scopes to the whole org. A short id or email
    resolves to one org_member: the account owner's calls ARE the whole
    org's, since there's nothing to scope down to; a regular member's
    calls are filtered to just what they uploaded.
    """
    q = (query or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="query is required.")

    oid = parse_org_id(q)
    if oid:
        return oid, None, {"org_id": oid, "user_id": None, "role": None, "scope": "org"}

    sid = _parse_short_id(q)
    with db.connection() as conn:
        if sid is not None:
            row = conn.execute(
                """
                SELECT org_id, user_id, role, first_name, last_name
                FROM org_members WHERE short_id = %s
                """,
                (sid,),
            ).fetchone()
            email = None
        else:
            rows = conn.execute(
                "SELECT * FROM public.admin_search_directory(%s)", (q,),
            ).fetchall()
            row = next(
                (r for r in rows or [] if (r.get("email") or "").strip().lower() == q.lower()),
                None,
            )
            email = row.get("email") if row else None

    org_id = parse_org_id((row or {}).get("org_id"))
    if not row or not org_id:
        raise HTTPException(
            status_code=404, detail="No match for that email, org id, or short id.",
        )
    user_id = str(row.get("user_id")) if row.get("user_id") else None
    role = (row.get("role") or "").strip().lower()
    matched = {
        "org_id": org_id,
        "user_id": user_id,
        "role": role or None,
        "name": _member_name(row),
        "email": email,
    }
    if role == "owner":
        return org_id, None, {**matched, "scope": "org"}
    return org_id, user_id, {**matched, "scope": "member"}


def _call_logs_rows(org_id: str, uploaded_by: str | None, *, limit: int) -> tuple[list[dict], int]:
    """Call rows for the resolved scope, newest first. Returns (rows, total_count)."""
    with org_scope(org_id):
        with db.connection() as conn:
            where = "c.org_id = %s"
            params: list = [org_id]
            if uploaded_by:
                where += " AND c.uploaded_by = %s"
                params.append(uploaded_by)
            total_row = conn.execute(
                f"SELECT COUNT(*) AS n FROM calls c WHERE {where}", params,
            ).fetchone()
            rows = conn.execute(
                f"""
                SELECT c.id, c.filename, c.job_id, c.created_at, c.audio_seconds,
                       c.uploaded_by, c.deleted_at, c.deleted_by,
                       pg_column_size(c.raw_json) AS raw_json_bytes
                FROM calls c
                WHERE {where}
                ORDER BY c.created_at DESC
                LIMIT %s
                """,
                [*params, limit],
            ).fetchall()
            audit_size_rows = conn.execute(
                f"""
                SELECT a.call_id, SUM(pg_column_size(a.findings)) AS n
                FROM audits a JOIN calls c ON c.id = a.call_id
                WHERE {where}
                GROUP BY a.call_id
                """,
                params,
            ).fetchall()
            member_rows = conn.execute(
                "SELECT user_id, first_name, last_name, short_id FROM org_members WHERE org_id = %s",
                (org_id,),
            ).fetchall()
    names = {str(r["user_id"]): _member_name(r) for r in member_rows or [] if r.get("user_id")}
    short_ids = {
        str(r["user_id"]): r.get("short_id") for r in member_rows or [] if r.get("user_id")
    }
    audit_bytes_by_call = {
        int(r["call_id"]): int(r.get("n") or 0)
        for r in audit_size_rows or []
        if r.get("call_id") is not None
    }
    out = []
    for row in rows or []:
        call_id = int(row["id"])
        uploader = row.get("uploaded_by")
        deleted_by = row.get("deleted_by")
        out.append(
            {
                "call_id": call_id,
                "filename": row.get("filename"),
                "created_at": _json_value(row.get("created_at")),
                "audio_seconds": row.get("audio_seconds"),
                "mode": _call_mode(row.get("job_id")),
                "uploaded_by": names.get(str(uploader)) if uploader else None,
                "data_size_bytes": int(row.get("raw_json_bytes") or 0)
                + audit_bytes_by_call.get(call_id, 0),
                "deleted": row.get("deleted_at") is not None,
                "deleted_by_short_id": short_ids.get(str(deleted_by)) if deleted_by else None,
            }
        )
    return out, int((total_row or {}).get("n") or 0)


def call_logs(query: str, limit: int | None = None) -> dict:
    """Search calls by email, org id, or short id.

    Owner id (or a bare org id) returns the whole org's calls; a regular
    member's id is scoped to only what that person uploaded.
    """
    org_id, uploaded_by, matched = _resolve_call_log_scope(query)
    cap = max(1, min(int(limit or _CALL_LOGS_LIMIT_DEFAULT), _CALL_LOGS_LIMIT_MAX))
    calls, total = _call_logs_rows(org_id, uploaded_by, limit=cap)
    return {
        "matched": matched,
        "calls": calls,
        "total_calls": total,
        "calls_truncated": total > len(calls),
    }


def call_logs_csv_rows(query: str) -> tuple[list[dict], dict]:
    """Same resolution as call_logs(), uncapped for export."""
    org_id, uploaded_by, matched = _resolve_call_log_scope(query)
    calls, _total = _call_logs_rows(org_id, uploaded_by, limit=_CALL_LOGS_LIMIT_MAX)
    return calls, matched


def rubric_for_org(org_id: str | None) -> dict:
    """Current active rubric weights for an org's Rubric tab (CR-14).

    Read-only GET — POST /api/admin/orgs/{org_id}/rubric (CR-13) saves a new
    version on the same resource (body {"weights": {...}}, same response shape).
    """
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, version, definition, updated_at
                FROM rubrics WHERE org_id = %s AND is_active LIMIT 1
                """,
                (oid,),
            ).fetchone()
    if row:
        definition = audit_store.decode_findings(row.get("definition"))
        source, rubric_id, version = "custom", str(row["id"]), int(row["version"])
        updated_at = _json_value(row.get("updated_at"))
    else:
        definition, source, rubric_id, version, updated_at = None, "legacy", None, None, None
    if not isinstance(definition, dict):
        definition = audit_store.load_v8_definition()
    weights = {d["id"]: d.get("weight") for d in qa_v8.list_dimensions(definition)}
    return {
        "org_id": oid,
        "source": source,
        "rubric_id": rubric_id,
        "version": version,
        "updated_at": updated_at,
        "weights": weights,
    }


def _normalize_rubric_weights(raw) -> dict[str, int]:
    ids = tuple(
        d["id"] for d in qa_v8.list_dimensions(audit_store.load_v8_definition())
    )
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="weights is required.")
    unknown = set(raw) - set(ids)
    missing = set(ids) - set(raw)
    if unknown or missing:
        raise HTTPException(
            status_code=400,
            detail="weights must include each scoring dimension.",
        )
    out: dict[str, int] = {}
    for did in ids:
        val = raw[did]
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise HTTPException(
                status_code=400, detail="Each weight must be a whole number.",
            )
        if val < 0 or int(val) != val:
            raise HTTPException(
                status_code=400, detail="Each weight must be a whole number.",
            )
        out[did] = int(val)
    if sum(out.values()) != _WEIGHT_SUM:
        raise HTTPException(status_code=400, detail="Weights must sum to 100.")
    return out


def save_org_rubric(org_id: str | None, weights, *, changed_by: str) -> dict:
    """Insert a new weighted rubric version for the target org (CR-13).

    Validates the four dimension weights before opening a connection so a
    bad payload cannot write anything. The deactivate + insert live in the
    same db.connection() transaction. changed_by is required, matching
    org_features.set_feature's convention for admin-mediated writes.

    CR-15: logs org, admin, old weights, new weights (timestamp is
    automatic via applog) via the same applog.event pattern already
    shipped for org_feature_changed/cache_cleared/call_deleted — rubrics
    has no changed_by column (no migration in this epic per the PRD), so
    this structured log is the audit trail, not a new Activity-feed row.
    """
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    actor = (changed_by or "").strip().lower()
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")
    normalized = _normalize_rubric_weights(weights)
    with org_scope(oid):
        with db.connection() as conn:
            saved = audit_store.insert_weighted_version(
                conn, org_id=oid, weights=normalized,
            )
    applog.event(
        log, "rubric_weights_saved",
        org_id=oid,
        rubric_id=saved["rubric_id"],
        version=saved["version"],
        changed_by=actor,
        old_weights=saved["previous_weights"],
        new_weights=saved["weights"],
    )
    return {
        "org_id": oid,
        "source": "custom",
        "rubric_id": saved["rubric_id"],
        "version": saved["version"],
        "updated_at": _json_value(saved.get("updated_at")),
        "weights": saved["weights"],
    }
