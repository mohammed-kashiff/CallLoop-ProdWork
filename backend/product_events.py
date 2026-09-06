"""Usage telemetry (AC-42/AC-43/AC-44, CallLoop-Observability-PRD §4).

track_event() is the persistence half of the PRD's "build it, don't buy
it" call — a fifth instance of the append-only, org-scoped events-table
pattern already proven by impersonation_log / password_reset_events /
org_features_history / call_trail. It sits alongside applog.event(), not
instead of it: call sites keep their existing structured log line and add
one track_event() call to also persist a queryable row.

Best-effort by design: every call site here is internal, not user input,
so a telemetry bug (a bad event_name, a transient DB error) must never
break the actual feature it's attached to — track_event() logs and
returns rather than raising. Contrast with password_events.record_event(),
which raises on invalid input because that module's callers are HTTP
handlers translating real request validation into a 400.

ALLOWED_EVENTS is deliberately closed, not open-ended — the PRD's own
risk section calls out "the table becomes a dumping ground" as the thing
to actively guard against. Adding an event means editing this set, not
just passing a new string.

Firm rule: properties is structural facts only (counts, ids, booleans,
small enums) — never transcript text, ticket message content, screenshot
content, or customer PII.
"""

from __future__ import annotations

import logging

from psycopg.types.json import Json

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.product_events")

ALLOWED_EVENTS = frozenset({
    "call_uploaded",
    "ticket_uploaded",
    "rubric_builder_opened",
    "rubric_saved",
    "churn_risk_viewed",
    "stakeholder_email_drafted",
    "flag_created",
    "flag_solved",
    "feedback_requested",
    "session_started",
    "upload_failed",
    "batch_partial_failure",
    "ticket_audit_opened",
})

# Events with a natural backend call site (AC-44) — fired via this
# module's track_event() directly from the route/service code already
# handling that action. Kept here only as documentation of the split;
# not enforced programmatically.
BACKEND_WIRED_EVENTS = frozenset({
    "call_uploaded",
    "ticket_uploaded",
    "rubric_saved",
    "stakeholder_email_drafted",
    "flag_created",
    "flag_solved",
    "feedback_requested",
    "upload_failed",
    "batch_partial_failure",
})

# Frontend-only interactions with no natural backend call site — these
# reach track_event() only via POST /api/events (AC-45/AC-46).
FRONTEND_ONLY_EVENTS = ALLOWED_EVENTS - BACKEND_WIRED_EVENTS


def track_event(
    org_id: str | None,
    user_id: str | None,
    event_name: str,
    properties: dict | None = None,
) -> None:
    """Best-effort: never raises. A telemetry failure must not break the
    feature it's attached to."""
    oid = parse_org_id(org_id)
    if not oid:
        applog.event(
            log, "product_event_skipped", level=logging.WARNING,
            event_name=event_name, reason="invalid_org_id",
        )
        return
    if event_name not in ALLOWED_EVENTS:
        applog.event(
            log, "product_event_skipped", level=logging.WARNING,
            event_name=event_name, org_id=oid, reason="not_in_allowlist",
        )
        return
    uid = parse_org_id(user_id) if user_id else None
    try:
        with org_scope(oid):
            with db.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO product_events (org_id, user_id, event_name, properties)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (oid, uid, event_name, Json(properties or {})),
                )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "product_event_write_failed", level=logging.WARNING,
            event_name=event_name, org_id=oid, error=applog.safe_exception_text(e),
        )
