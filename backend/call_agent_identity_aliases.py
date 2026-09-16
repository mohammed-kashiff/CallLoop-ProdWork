"""IN-22/23: resolve a provider's structured agent identifier (JustCall's
agent_email) to a real org_members.user_id, via an org owner's own
explicit mapping.

Parallel to ticket_agent_identity_aliases.py (IN-10, Intercom's
author.email) — same shape, same "no guessing" principle, calls instead
of tickets. Deliberately not the same table: `calls` is one row per call
(not per-turn like `ticket_messages`), so the backfill query here updates
`calls.agent_user_id` directly rather than joining through a separate
messages table.

identifier is stored and matched lowercased+trimmed, same as IN-10 —
JustCall's own casing on agent_email isn't guaranteed consistent call to
call.

Does not retroactively fix agent_user_id on calls ingested before a
mapping existed for turns whose identifier isn't yet known — set_alias()
below DOES backfill already-ingested, still-unresolved calls for the
exact identifier being mapped, in the same transaction, the same fix
TA-15/IN-10 needed found live for tickets — applied here from the start
rather than discovered later.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import audit_log
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.call_agent_identity_aliases")


def _normalize(identifier: str | None) -> str:
    return (identifier or "").strip().lower()


def list_aliases(org_id: str, provider: str | None = None) -> list[dict]:
    """Every identifier->person mapping this org has configured, for one
    provider or (when omitted) every provider."""
    with org_scope(org_id):
        with db.connection() as conn:
            if provider:
                rows = conn.execute(
                    """
                    SELECT provider, identifier, user_id, created_at, updated_at
                    FROM call_agent_identity_aliases
                    WHERE org_id = %s AND provider = %s
                    ORDER BY identifier
                    """,
                    (org_id, provider),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT provider, identifier, user_id, created_at, updated_at
                    FROM call_agent_identity_aliases
                    WHERE org_id = %s
                    ORDER BY provider, identifier
                    """,
                    (org_id,),
                ).fetchall()
    return [
        {
            "provider": r["provider"],
            "identifier": r["identifier"],
            "user_id": str(r["user_id"]),
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
            "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        }
        for r in rows or []
    ]


def set_alias(org_id: str, provider: str, identifier: str, user_id: str) -> dict:
    """Create or update this org's mapping for one provider+identifier.
    Caller must already have checked the caller is the org owner — this
    function itself does no permission check."""
    prov = (provider or "").strip().lower()
    ident = _normalize(identifier)
    uid = parse_org_id(user_id)
    if not prov:
        raise HTTPException(status_code=400, detail="provider is required.")
    if not ident:
        raise HTTPException(status_code=400, detail="identifier is required.")
    if not uid:
        raise HTTPException(status_code=400, detail="A valid user_id is required.")
    backfilled = 0
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO call_agent_identity_aliases
                    (org_id, provider, identifier, user_id, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (org_id, provider, identifier) DO UPDATE
                SET user_id = EXCLUDED.user_id, updated_at = now()
                """,
                (org_id, prov, ident, uid),
            )
            # Same fix TA-15/IN-10 needed found live for tickets, applied
            # here from day one: a mapping must retroactively resolve
            # calls already ingested under this exact identifier, not
            # only future ones — an agent mapped today must immediately
            # see their own past calls, not just calls from tomorrow.
            cur = conn.execute(
                """
                UPDATE calls
                SET agent_user_id = %s
                WHERE org_id = %s AND agent_user_id IS NULL
                  AND lower(agent_identifier) = %s
                """,
                (uid, org_id, ident),
            )
            backfilled = cur.rowcount
    applog.event(
        log, "call_agent_identity_alias_set",
        org_id=org_id, provider=prov, identifier=ident, backfilled_calls=backfilled,
    )
    audit_log.record(
        org_id, "alias.mapped",
        target_type="call_agent_identity_alias", target_id=f"{prov}:{ident}",
        after={"user_id": uid, "backfilled_calls": backfilled},
    )
    return {"provider": prov, "identifier": ident, "user_id": uid, "backfilled_calls": backfilled}


def delete_alias(org_id: str, provider: str, identifier: str) -> None:
    prov = (provider or "").strip().lower()
    ident = _normalize(identifier)
    if not prov or not ident:
        raise HTTPException(status_code=400, detail="provider and identifier are required.")
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                DELETE FROM call_agent_identity_aliases
                WHERE org_id = %s AND provider = %s AND identifier = %s
                """,
                (org_id, prov, ident),
            )
    applog.event(
        log, "call_agent_identity_alias_deleted",
        org_id=org_id, provider=prov, identifier=ident,
    )
    audit_log.record(
        org_id, "alias.removed",
        target_type="call_agent_identity_alias", target_id=f"{prov}:{ident}",
    )


def resolve_agent_user_id(org_id: str, provider: str, identifier: str | None) -> str | None:
    """The mapped user_id for this provider+identifier, or None if no
    alias is configured. Called once per JustCall ingest — a low-volume
    path (one call at a time), unlike tickets' batch resolver which
    exists to avoid one query per turn."""
    ident = _normalize(identifier)
    if not ident:
        return None
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT user_id FROM call_agent_identity_aliases
                WHERE org_id = %s AND provider = %s AND identifier = %s
                """,
                (org_id, provider, ident),
            ).fetchone()
    return str(row["user_id"]) if row else None


def list_unresolved_identifiers(org_id: str, provider: str) -> list[dict]:
    """Raw identifiers seen on real calls for this provider that never
    resolved to a user_id, with how many calls carry each — an org
    owner's own to-do list, mirroring ticket_agent_identity_aliases.py's
    same-named function for the Intercom path."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT agent_identifier, COUNT(*) AS call_count
                FROM calls
                WHERE org_id = %s AND source = %s
                  AND agent_user_id IS NULL
                  AND agent_identifier IS NOT NULL
                GROUP BY agent_identifier
                ORDER BY call_count DESC, agent_identifier
                """,
                (org_id, provider),
            ).fetchall()
    return [
        {"identifier": r["agent_identifier"], "turn_count": int(r["call_count"])}
        for r in rows or []
    ]


def suggest_identity_matches(org_id: str, provider: str) -> list[dict]:
    """list_unresolved_identifiers(), each with a suggested_user_id/
    suggested_name when this org has a real member whose email matches
    the identifier exactly (case-insensitive) — a JustCall agent's email
    often is their real CallLoop login too. None when there's no match;
    never a fuzzy/partial guess, only an exact email match, so a wrong
    suggestion is never worse than no suggestion. The owner still clicks
    to confirm — see set_alias(); this only pre-fills the pick."""
    unresolved = list_unresolved_identifiers(org_id, provider)
    if not unresolved:
        return []
    idents = [u["identifier"].lower() for u in unresolved]
    with db.connection(bypass_rls=True) as conn:
        rows = conn.execute(
            """
            SELECT user_id, email, first_name, last_name
            FROM org_directory
            WHERE org_id = %s AND lower(email) = ANY(%s)
            """,
            (org_id, idents),
        ).fetchall()
    by_email = {r["email"].lower(): r for r in rows or []}
    out = []
    for u in unresolved:
        match = by_email.get(u["identifier"].lower())
        name = None
        if match:
            name = " ".join(filter(None, [match["first_name"], match["last_name"]])).strip() or None
        out.append({
            **u,
            "suggested_user_id": str(match["user_id"]) if match else None,
            "suggested_name": name,
        })
    return out
