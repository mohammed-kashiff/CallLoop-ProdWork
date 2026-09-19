"""Per-ticket pipeline audit trail: ingest -> parse -> score -> serve,
including every failure with its cause, as queryable rows rather than
just log lines.

Mirrors call_trail.py (AC-24). record() is best-effort: a trail-write
failure must never break the pipeline step it was describing. It catches
its own exceptions and falls back to a plain applog line so the failure
itself isn't silently lost.

detail must not contain transcript quotes, screenshot text, credentials,
or other secrets — criterion rows store agent_user_id, verdict, and
evidence_verified only. Outbound HTTP calls are listed in detail["apis"]
as {method, endpoint} templates (no live ids, query strings, or tokens).
"""

from __future__ import annotations

import json
import logging
import uuid

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.ticket_trail")

# Templates only — never interpolate a live id, signed URL, or query string.
API_ANTHROPIC_MESSAGES = {
    "method": "POST", "endpoint": "https://api.anthropic.com/v1/messages",
}
API_INTERCOM_CONVERSATION = {
    "method": "GET", "endpoint": "https://api.intercom.io/conversations/{id}",
}
API_INTERCOM_TICKET = {
    "method": "GET", "endpoint": "https://api.intercom.io/tickets/{id}",
}
API_INTERCOM_ATTACHMENT = {
    "method": "GET",
    "endpoint": "https://*.intercomcdn.com/… (attachment; live URL not stored)",
}
API_TICKET_UPLOAD = {
    "method": "POST", "endpoint": "/api/tickets/upload",
}
API_TICKET_SCORE = {
    "method": "POST", "endpoint": "/api/tickets/{ticket_id}/score",
}


def with_apis(detail: dict | None, *apis: dict) -> dict:
    """Merge method/endpoint templates onto a detail dict. Drops query
    strings so a signed attachment URL can never land in the trail."""
    out = dict(detail or {})
    cleaned: list[dict] = []
    for item in apis:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "").strip().upper()
        endpoint = str(item.get("endpoint") or "").strip().split("?", 1)[0]
        if not method or not endpoint:
            continue
        cleaned.append({"method": method, "endpoint": endpoint})
    if cleaned:
        out["apis"] = cleaned
    return out


def intercom_fetch_apis(kind: str, linked: list | None = None) -> tuple[dict, ...]:
    kinds = {kind}
    for item in linked or []:
        if isinstance(item, (tuple, list)) and item:
            kinds.add(item[0])
    out: list[dict] = []
    if "conversation" in kinds:
        out.append(API_INTERCOM_CONVERSATION)
    if "ticket" in kinds:
        out.append(API_INTERCOM_TICKET)
    return tuple(out)


def _parse_ticket_id(ticket_id) -> str | None:
    try:
        return str(uuid.UUID(str(ticket_id or "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def agent_resolve_detail(turns: list[dict] | None) -> dict:
    """Counts only — not the raw speaker names."""
    resolved_ids: set[str] = set()
    unresolved_names: set[str] = set()
    for t in turns or []:
        if (t.get("speaker") or "") != "agent":
            continue
        uid = t.get("agent_user_id")
        if uid:
            resolved_ids.add(str(uid))
            continue
        name = (t.get("speaker_name") or "").strip()
        if name:
            unresolved_names.add(name)
    return {"resolved": len(resolved_ids), "unresolved": len(unresolved_names)}


def record(
    ticket_id,
    org_id: str | None,
    stage: str,
    status: str,
    *,
    detail: dict | None = None,
    error: str | None = None,
) -> None:
    """One row: ticket_id/org_id/stage/status, plus optional JSONB detail
    and a plain-text error (only meaningful when status='failed').

    status must be 'started' | 'succeeded' | 'failed' — an unexpected
    value is coerced to 'failed' with the original value folded into
    detail, rather than raising and losing the event entirely.
    """
    tid = _parse_ticket_id(ticket_id)
    oid = parse_org_id(org_id)
    if not tid:
        applog.event(
            log, "ticket_trail_skipped", level=logging.WARNING,
            stage=stage, reason="no_ticket_id",
        )
        return
    if not oid:
        applog.event(
            log, "ticket_trail_skipped", level=logging.WARNING,
            ticket_id=tid, stage=stage, reason="no_org_id",
        )
        return
    if status not in ("started", "succeeded", "failed"):
        detail = {**(detail or {}), "_invalid_status": status}
        status = "failed"
    try:
        with org_scope(oid):
            with db.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO ticket_pipeline_events (
                        org_id, ticket_id, stage, status, detail, error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        oid, tid, stage, status,
                        json.dumps(detail) if detail is not None else None,
                        error,
                    ),
                )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "ticket_trail_write_failed", level=logging.WARNING,
            ticket_id=tid, stage=stage, status=status,
            error=applog.safe_exception_text(e),
        )


def history(ticket_id, org_id: str) -> list[dict]:
    """Full trail for one ticket, chronological."""
    tid = _parse_ticket_id(ticket_id)
    oid = parse_org_id(org_id)
    if not tid or not oid:
        return []
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT stage, status, detail, error, created_at
                FROM ticket_pipeline_events
                WHERE ticket_id = %s AND org_id = %s
                ORDER BY created_at ASC
                """,
                (tid, oid),
            ).fetchall()
    out: list[dict] = []
    for row in rows or []:
        created_at = row.get("created_at")
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = None
        out.append(
            {
                "stage": row.get("stage"),
                "status": row.get("status"),
                "detail": detail,
                "error": row.get("error"),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return out
