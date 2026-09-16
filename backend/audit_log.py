"""audit_log.py — AC-63/AC-65: durable, append-only record of who did what.

Separate from product_events.py (product analytics, closed allowlist of
events like "call_uploaded") and from audit_store.py/ticket_audit_store.py
(scoring output, a different concept entirely). This is a security/
compliance trail: real state-changing actions only (role changed,
credential saved, rubric activated, alias mapped, export generated...),
never plain reads — those stay as regular actor-tagged log lines (AC-64),
so this table doesn't balloon with noise.

record() is best-effort, matching product_events.track_event()'s own
rule: a telemetry bug must never break the actual action it's attached
to. Never raises into the caller — logs and returns on any failure.

actor_id/actor_email/ip_address default to whatever's bound on the
request-scoped context (org_ids.py, AC-64) so most call sites never pass
them explicitly; a caller may still override for a background job with
no request context of its own.
"""

from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import applog
from . import db
from .org_ids import bound_actor_email, bound_actor_ip, bound_user_id, org_scope, parse_org_id

log = logging.getLogger("callproof.audit_log")


def record(
    org_id: str,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | None = None,
    before: dict | None = None,
    after: dict | None = None,
    actor_id: str | None = None,
    actor_email: str | None = None,
) -> None:
    """Write one durable audit_log row. Never raises — a failure here must
    never break the real action it's attached to."""
    oid = parse_org_id(org_id)
    if not oid:
        return
    try:
        with org_scope(oid):
            with db.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_log (
                        org_id, actor_id, actor_email, action, target_type,
                        target_id, before, after, ip_address, request_id
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        oid,
                        parse_org_id(actor_id) or bound_user_id(),
                        actor_email or bound_actor_email(),
                        action,
                        target_type,
                        str(target_id) if target_id is not None else None,
                        Json(before) if before is not None else None,
                        Json(after) if after is not None else None,
                        bound_actor_ip(),
                        applog.bound_request_id(),
                    ),
                )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "audit_log_write_failed", level=logging.ERROR,
            org_id=oid, action=action, error=applog.safe_exception_text(e),
        )
