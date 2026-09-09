"""IN-10: resolve a provider's structured agent identifier (Intercom's
author.email) to a real org_members.user_id, via an org owner's own
explicit mapping.

Parallel to ticket_agent_aliases.py (TA-15, freeform PDF display names),
not a variant of it — that table is hard-keyed on (org_id, display_name)
and every resolver takes display_name as its lookup parameter, not an
arbitrary identifier. Keying on email needed an actual schema decision
(0034): a new table, generic on (org_id, provider, identifier), rather
than retrofitting the shipped PDF table's contract.

identifier is stored and matched lowercased+trimmed — email addresses
are case-insensitive in practice, and Intercom's own casing isn't
guaranteed consistent turn to turn.

Does not retroactively fix agent_user_id on tickets ingested before a
mapping existed — only future ingestions benefit, same accepted v1
boundary TA-15 already documents for the PDF path.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.ticket_agent_identity_aliases")


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
                    FROM ticket_agent_identity_aliases
                    WHERE org_id = %s AND provider = %s
                    ORDER BY identifier
                    """,
                    (org_id, provider),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT provider, identifier, user_id, created_at, updated_at
                    FROM ticket_agent_identity_aliases
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
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO ticket_agent_identity_aliases
                    (org_id, provider, identifier, user_id, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (org_id, provider, identifier) DO UPDATE
                SET user_id = EXCLUDED.user_id, updated_at = now()
                """,
                (org_id, prov, ident, uid),
            )
    applog.event(
        log, "ticket_agent_identity_alias_set",
        org_id=org_id, provider=prov, identifier=ident,
    )
    return {"provider": prov, "identifier": ident, "user_id": uid}


def delete_alias(org_id: str, provider: str, identifier: str) -> None:
    prov = (provider or "").strip().lower()
    ident = _normalize(identifier)
    if not prov or not ident:
        raise HTTPException(status_code=400, detail="provider and identifier are required.")
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                DELETE FROM ticket_agent_identity_aliases
                WHERE org_id = %s AND provider = %s AND identifier = %s
                """,
                (org_id, prov, ident),
            )
    applog.event(
        log, "ticket_agent_identity_alias_deleted",
        org_id=org_id, provider=prov, identifier=ident,
    )


def resolve_agent_user_id(org_id: str, provider: str, identifier: str | None) -> str | None:
    """The mapped user_id for this provider+identifier, or None if no
    alias is configured. A per-ticket batch lookup (resolve_agent_user_ids)
    is used instead when ingesting a whole ticket/conversation, to avoid
    one query per turn."""
    ident = _normalize(identifier)
    if not ident:
        return None
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT user_id FROM ticket_agent_identity_aliases
                WHERE org_id = %s AND provider = %s AND identifier = %s
                """,
                (org_id, provider, ident),
            ).fetchone()
    return str(row["user_id"]) if row else None


def resolve_agent_user_ids(org_id: str, provider: str, identifiers: set[str]) -> dict[str, str]:
    """Batch form of resolve_agent_user_id — one query per ingest instead
    of one per agent turn. {identifier: user_id} for whichever of the
    given identifiers this org has a mapping for; identifiers with no
    alias are simply absent from the result. Keys are the normalized
    (lowercased+trimmed) form, matching what callers already extract
    from an author dict."""
    idents = sorted({_normalize(i) for i in identifiers if _normalize(i)})
    if not idents:
        return {}
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT identifier, user_id FROM ticket_agent_identity_aliases
                WHERE org_id = %s AND provider = %s AND identifier = ANY(%s)
                """,
                (org_id, provider, idents),
            ).fetchall()
    return {r["identifier"]: str(r["user_id"]) for r in rows or []}
