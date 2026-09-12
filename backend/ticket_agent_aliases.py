"""TA-15: resolve a ticket PDF's raw agent display name to a real
org_members.user_id, via an org owner's own explicit mapping.

The gap this closes: ticket_pdf_parser.py can identify an agent turn's
role and raw display name ("Kashif", "Tanu") from the PDF text, but has
no way to resolve that name to a real user — org_members stores no
display name to match against, only a Supabase user_id. Every agent
turn's agent_user_id was NULL regardless of how many different named
agents actually replied, which meant TA-8's multi-agent attribution
mechanism (correct and fully tested) had nothing to work with on any
PDF-sourced ticket.

This does not retroactively fix agent_user_id on tickets ingested before
a mapping existed — only future ingestions benefit once an org owner
maps a name. A known, accepted v1 boundary (TA-15's own ticket).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.ticket_agent_aliases")


def list_aliases(org_id: str) -> list[dict]:
    """Every name->person mapping this org has configured."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT display_name, user_id, created_at, updated_at
                FROM ticket_agent_aliases
                WHERE org_id = %s
                ORDER BY display_name
                """,
                (org_id,),
            ).fetchall()
    return [
        {
            "display_name": r["display_name"],
            "user_id": str(r["user_id"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows or []
    ]


def list_unresolved_agent_names(org_id: str) -> list[dict]:
    """Raw display names seen on real agent turns that never resolved to
    a user_id, with how many turns carry each — an org owner's own to-do
    list for filling in aliases, not a guess at what names might exist."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT speaker_display_name, COUNT(*) AS turn_count
                FROM ticket_messages
                WHERE org_id = %s AND speaker = 'agent'
                  AND agent_user_id IS NULL
                  AND speaker_display_name IS NOT NULL
                GROUP BY speaker_display_name
                ORDER BY turn_count DESC, speaker_display_name
                """,
                (org_id,),
            ).fetchall()
    return [
        {"display_name": r["speaker_display_name"], "turn_count": int(r["turn_count"])}
        for r in rows or []
    ]


def set_alias(org_id: str, display_name: str, user_id: str) -> dict:
    """Create or update this org's mapping for one display name. Caller
    must already have checked the caller is the org owner — this
    function itself does no permission check."""
    name = (display_name or "").strip()
    uid = parse_org_id(user_id)
    if not name:
        raise HTTPException(status_code=400, detail="display_name is required.")
    if not uid:
        raise HTTPException(status_code=400, detail="A valid user_id is required.")
    backfilled = 0
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO ticket_agent_aliases (org_id, display_name, user_id, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (org_id, display_name) DO UPDATE
                SET user_id = EXCLUDED.user_id, updated_at = now()
                """,
                (org_id, name, uid),
            )
            # Same fix as ticket_agent_identity_aliases.set_alias, same
            # root cause: resolution only ever applied at original ingest
            # time, so mapping a name never touched turns already sitting
            # there — an agent opening a ticket they were literally on
            # saw a blank thread (TA-12's own-turns-only filter correctly
            # found nothing, because nothing was ever theirs on record).
            # Backfill every already-ingested, still-unresolved turn for
            # this exact name now, same transaction as the alias itself.
            cur = conn.execute(
                """
                UPDATE ticket_messages m
                SET agent_user_id = %s
                FROM tickets t
                WHERE t.id = m.ticket_id AND t.org_id = m.org_id
                  AND m.org_id = %s AND t.source = 'pdf_upload'
                  AND m.speaker = 'agent' AND m.agent_user_id IS NULL
                  AND m.speaker_display_name = %s
                """,
                (uid, org_id, name),
            )
            backfilled = cur.rowcount
    applog.event(
        log, "ticket_agent_alias_set",
        org_id=org_id, display_name=name, backfilled_turns=backfilled,
    )
    return {"display_name": name, "user_id": uid, "backfilled_turns": backfilled}


def delete_alias(org_id: str, display_name: str) -> None:
    name = (display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="display_name is required.")
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                "DELETE FROM ticket_agent_aliases WHERE org_id = %s AND display_name = %s",
                (org_id, name),
            )
    applog.event(log, "ticket_agent_alias_deleted", org_id=org_id, display_name=name)


def resolve_agent_user_id(org_id: str, display_name: str | None) -> str | None:
    """The mapped user_id for this raw display name, or None if no alias
    is configured. Called once per agent turn at ingestion time — a
    per-ticket batch lookup (resolve_agent_user_ids) is used instead
    when ingesting a whole ticket, to avoid one query per turn."""
    name = (display_name or "").strip()
    if not name:
        return None
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM ticket_agent_aliases WHERE org_id = %s AND display_name = %s",
                (org_id, name),
            ).fetchone()
    return str(row["user_id"]) if row else None


def list_org_agents(org_id: str) -> list[dict]:
    """This org's members, for a mapping-picker UI. Names are nullable
    (captured once at signup — see 0011_org_members_names_and_directory_view.py),
    so a caller should fall back to showing the user_id when both are blank."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT user_id, first_name, last_name, role
                FROM org_members
                WHERE org_id = %s
                ORDER BY first_name, last_name
                """,
                (org_id,),
            ).fetchall()
    return [
        {
            "user_id": str(r["user_id"]),
            "first_name": r["first_name"],
            "last_name": r["last_name"],
            "role": r["role"],
        }
        for r in rows or []
    ]


def resolve_agent_user_ids(org_id: str, display_names: set[str]) -> dict[str, str]:
    """Batch form of resolve_agent_user_id — one query per ingest instead
    of one per agent turn. {display_name: user_id} for whichever of the
    given names this org has a mapping for; names with no alias are
    simply absent from the result."""
    names = sorted({(n or "").strip() for n in display_names if (n or "").strip()})
    if not names:
        return {}
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT display_name, user_id FROM ticket_agent_aliases
                WHERE org_id = %s AND display_name = ANY(%s)
                """,
                (org_id, names),
            ).fetchall()
    return {r["display_name"]: str(r["user_id"]) for r in rows or []}
