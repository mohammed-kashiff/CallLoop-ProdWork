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

list_unresolved_identifiers()/suggest_identity_matches() (competitive
gap found against MaestroQA's own Intercom integration, which syncs
Intercom's Admins object directly and so can likely auto-attribute from
day one): speaker_display_name (TA-17) now carries an agent turn's raw
identifier for every ticket, Intercom included, so an unresolved
Intercom email can be listed the same way TA-15 already lists unresolved
PDF names. suggest_identity_matches() goes one step further — an
Intercom agent's email often *is* their real CallLoop login too, so a
case-insensitive match against this org's own member emails gives an
owner a one-click confirmation instead of hunting for the right
user_id. org_members itself has no email column (0011 put email on
org_directory instead, joined from auth.users) and org_directory is
deliberately REVOKEd from callproof_app (that migration's own comment:
"Do not expose via an API without per-org filtering") — reading it here
needs bypass_rls, the same narrowly-scoped-escape-hatch category as
org_vault.py's poller listing, made safe the same way: an explicit
org_id filter in the query itself, so no cross-tenant email can ever
leave this org's own scope. Never auto-applies a match — an owner still
confirms it, same "no guessing" principle IN-10's whole design already
rests on.
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
    backfilled = 0
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
            # Found live: a teammate opening a ticket they were literally
            # the agent on saw a blank thread, because resolution only
            # ever applied at original ingest time — mapping an alias
            # never touched turns already sitting there. TA-12's viewer
            # filter (a non-owner sees only their own agent_user_id) then
            # correctly showed nothing, which is the wrong correct answer:
            # an agent must be able to see their own past work the moment
            # they're mapped, not only their future turns. Backfill every
            # already-ingested, still-unresolved turn for this exact
            # identifier now, in the same transaction as the alias itself.
            cur = conn.execute(
                """
                UPDATE ticket_messages m
                SET agent_user_id = %s
                FROM tickets t
                WHERE t.id = m.ticket_id AND t.org_id = m.org_id
                  AND m.org_id = %s AND t.source = %s
                  AND m.speaker = 'agent' AND m.agent_user_id IS NULL
                  AND lower(m.speaker_display_name) = %s
                """,
                (uid, org_id, f"{prov}_api", ident),
            )
            backfilled = cur.rowcount
    applog.event(
        log, "ticket_agent_identity_alias_set",
        org_id=org_id, provider=prov, identifier=ident, backfilled_turns=backfilled,
    )
    return {"provider": prov, "identifier": ident, "user_id": uid, "backfilled_turns": backfilled}


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


def list_unresolved_identifiers(org_id: str, provider: str) -> list[dict]:
    """Raw identifiers seen on real agent turns for this provider that
    never resolved to a user_id, with how many turns carry each — an org
    owner's own to-do list, mirroring ticket_agent_aliases.py's
    list_unresolved_agent_names() for the PDF path. Scoped to tickets
    whose source is this provider's (e.g. "intercom_api" for "intercom")
    since speaker_display_name is populated for every ticket source
    (TA-17) but only a provider like Intercom's is a structured,
    matchable identifier (an email), not a freeform PDF name."""
    source = f"{provider}_api"
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT m.speaker_display_name, COUNT(*) AS turn_count
                FROM ticket_messages m
                JOIN tickets t ON t.id = m.ticket_id AND t.org_id = m.org_id
                WHERE m.org_id = %s AND t.source = %s
                  AND m.speaker = 'agent'
                  AND m.agent_user_id IS NULL
                  AND m.speaker_display_name IS NOT NULL
                GROUP BY m.speaker_display_name
                ORDER BY turn_count DESC, m.speaker_display_name
                """,
                (org_id, source),
            ).fetchall()
    return [
        {"identifier": r["speaker_display_name"], "turn_count": int(r["turn_count"])}
        for r in rows or []
    ]


def suggest_identity_matches(org_id: str, provider: str) -> list[dict]:
    """list_unresolved_identifiers(), each with a suggested_user_id/
    suggested_name when this org has a real member whose email matches
    the identifier exactly (case-insensitive) — an Intercom agent's
    email often is their real CallLoop login too. None when there's no
    match; never a fuzzy/partial guess, only an exact email match, so a
    wrong suggestion is never worse than no suggestion. The owner still
    clicks to confirm — see set_alias(); this only pre-fills the pick."""
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
