"""IN-10 HTTP surface: org owner manages the mapping from a provider's
structured agent identifier (Intercom's author.email) to a real
org_members.user_id.

Separate module from ticket_agent_aliases_api.py (TA-15's own HTTP
surface for the PDF path's freeform display names) — same reasoning
that file gives for staying out of ticket_api.py: a different
identifier kind, kept apart rather than bolted onto an existing route
shape. Registered under /api/tickets/agent-identity-aliases, alongside
but distinct from /api/tickets/agent-aliases.

Every mutation is owner-only (auth.require_owner) — same tier as the
rubric builder, org_features toggles, and TA-15's own alias routes.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import auth
from . import ticket_agent_aliases
from . import ticket_agent_identity_aliases
from .intercom_oauth import PROVIDER as INTERCOM_PROVIDER

log = logging.getLogger("callproof.ticket_agent_identity_aliases_api")


class SetIdentityAliasBody(BaseModel):
    provider: str = INTERCOM_PROVIDER
    identifier: str
    user_id: str


def list_agent_identity_aliases(request: Request):
    """Owner-only: current mappings, unresolved identifiers (each with a
    suggested_user_id/suggested_name when this org has a member whose
    email matches exactly — see ticket_agent_identity_aliases.
    suggest_identity_matches for why this is possible now, unlike TA-15's
    PDF names), and this org's roster for a manual pick when there's no
    suggestion or it's wrong."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    return {
        "aliases": ticket_agent_identity_aliases.list_aliases(org_id),
        "unresolved": ticket_agent_identity_aliases.suggest_identity_matches(
            org_id, INTERCOM_PROVIDER,
        ),
        "members": ticket_agent_aliases.list_org_agents(org_id),
    }


def set_agent_identity_alias(request: Request, body: SetIdentityAliasBody):
    """Owner-only: create or update this org's mapping for one
    provider+identifier. Applies to future ingestions only — does not
    retroactively backfill agent_user_id on tickets already ingested."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    result = ticket_agent_identity_aliases.set_alias(
        org_id, body.provider, body.identifier, body.user_id,
    )
    applog.event(
        log, "ticket_agent_identity_alias_set_via_api",
        org_id=org_id, provider=result["provider"], identifier=result["identifier"],
    )
    return result


def delete_agent_identity_alias(request: Request, provider: str, identifier: str):
    """Owner-only: remove a mapping. Future ingestions of that identifier
    go back to unresolved; already-ingested turns are untouched."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    prov = (provider or "").strip()
    ident = (identifier or "").strip()
    if not prov or not ident:
        raise HTTPException(status_code=400, detail="provider and identifier are required.")
    ticket_agent_identity_aliases.delete_alias(org_id, prov, ident)
    return {"ok": True}


def register(app) -> None:
    app.add_api_route(
        "/api/tickets/agent-identity-aliases", list_agent_identity_aliases, methods=["GET"],
    )
    app.add_api_route(
        "/api/tickets/agent-identity-aliases", set_agent_identity_alias, methods=["POST"],
    )
    app.add_api_route(
        "/api/tickets/agent-identity-aliases/{provider}/{identifier}",
        delete_agent_identity_alias, methods=["DELETE"],
    )
