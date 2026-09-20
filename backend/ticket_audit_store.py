"""Persist and read ticket scorecards (TA-11, rebuilt TA-21/TA-28).

Separate from audit_store.py — that module is the call engine's audits
table (call_id BIGINT). Ticket audits live in ticket_audits and are
org-scoped through org_scope() / RLS like every other ticket write.

One row per (ticket_id, agent_user_id) — TA-28. fetch_all() is the
rescoring guard's input: which agents already have a stored row on this
ticket. That guard is per-agent, not per-ticket: a row existing for
Agent A blocks Claude from re-running on A without
enable_ticket_rescoring, but a newly-resolved Agent B with no row yet
still gets a real first score even though the ticket has "already been
audited" in the old, whole-ticket sense.
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


def _row_to_dict(row) -> dict:
    """Flattened to the same shape ticket_scoring.score_ticket_per_agent()
    returns ({"agent_user_id", "score", "findings": [...], "spans": [...]}
    at the top level) — the stored JSONB column is named `findings` for
    historical reasons but its *content* is upsert_many()'s payload dict
    ({"findings": [...], "spans": [...]}), spread in here rather than left
    nested, so a caller never has to know whether a given agent's result
    came from a fresh Claude run or a stored row."""
    payload = _as_dict(row["findings"])
    return {
        "id": str(row["id"]),
        "agent_user_id": str(row["agent_user_id"]),
        "score": row["score"],
        **payload,
        "requested_by": str(row["requested_by"]) if row["requested_by"] else None,
        "triggered_by": row["triggered_by"],
        "created_at": _iso(row["created_at"]),
        "updated_at": _iso(row["updated_at"]),
    }


def fetch_all(ticket_id: str, org_id: str) -> list[dict]:
    """Every stored scorecard on this ticket, one per agent already
    scored — empty if the ticket has never been scored for anyone yet."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, agent_user_id, score, findings, requested_by,
                       triggered_by, created_at, updated_at
                FROM ticket_audits
                WHERE ticket_id = %s AND org_id = %s
                ORDER BY created_at
                """,
                (ticket_id, org_id),
            ).fetchall()
    return [_row_to_dict(r) for r in rows or []]


def fetch_for_agent(ticket_id: str, org_id: str, agent_user_id: str) -> dict | None:
    """This one agent's stored scorecard on this ticket, or None if they
    haven't been scored yet — the per-agent rescoring-guard check."""
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, agent_user_id, score, findings, requested_by,
                       triggered_by, created_at, updated_at
                FROM ticket_audits
                WHERE ticket_id = %s AND org_id = %s AND agent_user_id = %s
                """,
                (ticket_id, org_id, agent_user_id),
            ).fetchone()
    return _row_to_dict(row) if row else None


def upsert_many(
    ticket_id: str,
    org_id: str,
    agent_results: list[dict],
    *,
    requested_by: str | None = None,
    triggered_by: str = "manual",
) -> list[str]:
    """INSERT one row per agent result, or UPDATE that agent's existing
    row on an allowed re-score. agent_results is score_ticket_per_agent's
    output: each dict must carry agent_user_id/score/findings (plus
    whatever else the caller wants persisted as findings — response
    timeliness, spans, etc. are folded in by the caller before this).

    triggered_by (IN-31) is one value for the whole call — every agent
    scored in the same score_ticket_route/auto_audit_ticket invocation
    was triggered the same way — never derived per-agent from the
    result dict, and excluded from the stored JSONB payload since it's
    a real column now, not scoring output."""
    actor = parse_org_id(requested_by)
    sql = """
                INSERT INTO ticket_audits (
                    id, org_id, ticket_id, agent_user_id, score, findings,
                    requested_by, triggered_by, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now(), now())
                ON CONFLICT (ticket_id, agent_user_id) DO UPDATE SET
                    score = excluded.score,
                    findings = excluded.findings,
                    requested_by = COALESCE(excluded.requested_by, ticket_audits.requested_by),
                    triggered_by = excluded.triggered_by,
                    updated_at = now()
                RETURNING id
                """
    ids: list[str] = []
    with org_scope(org_id):
        for result in agent_results:
            agent_user_id = result["agent_user_id"]
            score = result.get("score")
            payload = {
                k: v for k, v in result.items()
                if k not in ("agent_user_id", "score", "triggered_by")
            }
            audit_id = str(uuid.uuid4())
            params_with_actor = (
                audit_id, org_id, ticket_id, agent_user_id, score, Json(payload),
                actor, triggered_by,
            )
            params_without_actor = (
                audit_id, org_id, ticket_id, agent_user_id, score, Json(payload),
                None, triggered_by,
            )
            # Each agent's upsert gets its own connection/transaction — a
            # ForeignKeyViolation rollback on one agent must never discard
            # another agent's already-committed row from earlier in the loop.
            with db.connection() as conn:
                try:
                    row = conn.execute(sql, params_with_actor).fetchone()
                except ForeignKeyViolation:
                    log.debug("ticket audit requested_by omitted; not an org member")
                    conn.rollback()
                    row = conn.execute(sql, params_without_actor).fetchone()
            ids.append(str(row["id"]))
    return ids
