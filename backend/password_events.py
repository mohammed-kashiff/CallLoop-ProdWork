"""Password reset/change audit trail (AC-16). Append-only.

Investigated before building (per the ticket): Supabase's own
auth.audit_log_entries has an ip_address column but is empty for this
project — 0 rows despite real activity, so it can't be relayed. Supabase
Auth Hooks have no "password changed" event to attach to either. So this
module is the capture point: called from the admin reset actions (already
ours) and from a self-report the frontend sends right after Supabase itself
confirms a self-service change.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.password_events")

EVENT_TYPES = ("self_service", "admin_reset_email", "admin_direct_reset")


def _parse_uid(user_id) -> str | None:
    try:
        return str(uuid.UUID(str(user_id)))
    except (ValueError, TypeError, AttributeError):
        return None


def org_for_user(user_id: str) -> str | None:
    """Which org this user belongs to, via org_members. None if not found."""
    uid = _parse_uid(user_id)
    if not uid:
        return None
    with db.connection() as conn:
        row = conn.execute(
            "SELECT org_id FROM org_members WHERE user_id = %s", (uid,),
        ).fetchone()
    return parse_org_id((row or {}).get("org_id"))


def record_event(
    *,
    user_id: str,
    event_type: str,
    org_id: str | None = None,
    actor_email: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Append one row. Resolves org_id from org_members when not given.

    Silently skips (with a log line) if the user has no org membership —
    this is an audit trail, not a control; it must never block the actual
    password action over a lookup miss.
    """
    uid = _parse_uid(user_id)
    if not uid:
        raise HTTPException(status_code=400, detail="Invalid user_id.")
    if event_type not in EVENT_TYPES:
        raise HTTPException(status_code=400, detail="Unknown event_type.")
    oid = parse_org_id(org_id) or org_for_user(uid)
    if not oid:
        applog.event(
            log, "password_event_skipped", level=logging.WARNING,
            user_id=uid, event_type=event_type, reason="no_org_membership",
        )
        return
    email = (actor_email or "").strip().lower() or None
    with org_scope(oid):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO password_reset_events (
                    org_id, user_id, event_type, actor_email, ip_address
                )
                VALUES (%s, %s, %s, %s, %s)
                """,
                (oid, uid, event_type, email, ip_address),
            )
    applog.event(
        log, "password_event_recorded",
        org_id=oid, user_id=uid, event_type=event_type,
    )


def history_for_user(user_id: str) -> list[dict]:
    """Chronological password events for this user, most recent first."""
    uid = _parse_uid(user_id)
    if not uid:
        return []
    oid = org_for_user(uid)
    if not oid:
        return []
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT event_type, actor_email, ip_address, created_at
                FROM password_reset_events
                WHERE org_id = %s AND user_id = %s
                ORDER BY created_at DESC
                """,
                (oid, uid),
            ).fetchall()
    out: list[dict] = []
    for row in rows or []:
        created_at = row.get("created_at")
        out.append(
            {
                "event_type": row.get("event_type"),
                "actor_email": row.get("actor_email"),
                "ip_address": row.get("ip_address"),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return out
