"""Persist and read ticket scorecards (TA-11).

Separate from audit_store.py — that module is the call engine's audits
table (call_id BIGINT). Ticket audits live in ticket_audits and are
org-scoped through org_scope() / RLS like every other ticket write.

fetch_latest() is the guard's input: a row means this ticket has already
been audited. upsert() is only called on a first score, or on a re-score
the org has explicitly allowed via enable_ticket_rescoring.
"""

from __future__ import annotations

import json
import logging
import uuid

from psycopg.errors import ForeignKeyViolation
from psycopg.types.json import Json

from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.ticket_audit_store")


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    return {}


def fetch_latest(ticket_id: str, org_id: str) -> dict | None:
    """The stored scorecard for this ticket, or None if never audited."""
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, score, findings, requested_by, created_at
                FROM ticket_audits
                WHERE ticket_id = %s AND org_id = %s
                """,
                (ticket_id, org_id),
            ).fetchone()
    if not row:
        return None
    return {
        "id": str(row["id"]),
        "score": row["score"],
        "findings": _as_dict(row["findings"]),
        "requested_by": str(row["requested_by"]) if row["requested_by"] else None,
        "created_at": _iso(row["created_at"]),
    }


def upsert(
    ticket_id: str,
    org_id: str,
    findings: dict,
    *,
    requested_by: str | None = None,
) -> str:
    """INSERT, or UPDATE the one row for this ticket on an allowed re-score."""
    audit_id = str(uuid.uuid4())
    score = findings.get("score") if isinstance(findings, dict) else None
    actor = parse_org_id(requested_by)
    payload = findings if isinstance(findings, dict) else {}
    params_with_actor = (audit_id, org_id, ticket_id, score, Json(payload), actor)
    params_without_actor = (audit_id, org_id, ticket_id, score, Json(payload), None)
    sql = """
                INSERT INTO ticket_audits (
                    id, org_id, ticket_id, score, findings, requested_by, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (ticket_id) DO UPDATE SET
                    score = excluded.score,
                    findings = excluded.findings,
                    requested_by = COALESCE(excluded.requested_by, ticket_audits.requested_by),
                    created_at = now()
                RETURNING id
                """
    with org_scope(org_id):
        with db.connection() as conn:
            try:
                row = conn.execute(sql, params_with_actor).fetchone()
            except ForeignKeyViolation:
                log.debug("ticket audit requested_by omitted; not an org member")
                conn.rollback()
                row = conn.execute(sql, params_without_actor).fetchone()
    return str(row["id"])
