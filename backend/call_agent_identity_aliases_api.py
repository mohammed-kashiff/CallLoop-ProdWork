"""IN-22/23 HTTP surface: org owner manages the mapping from a provider's
structured agent identifier (JustCall's agent_email) to a real
org_members.user_id.

Parallel to ticket_agent_identity_aliases_api.py (IN-10) — same shape,
calls instead of tickets. Registered under /api/calls/agent-identity-
aliases. Every mutation is owner-only (auth.require_owner) — same tier
as the rubric builder, org_features toggles, and every other alias
route in this codebase.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import auth
from . import call_agent_identity_aliases
from . import ticket_agent_aliases

log = logging.getLogger("callproof.call_agent_identity_aliases_api")

_JUSTCALL_PROVIDER = "justcall"


class SetCallIdentityAliasBody(BaseModel):
    provider: str = _JUSTCALL_PROVIDER
    identifier: str
    user_id: str


def list_call_agent_identity_aliases(request: Request):
    """Owner-only: current mappings, unresolved identifiers (each with a
    suggested_user_id/suggested_name when this org has a member whose
    email matches exactly), and this org's roster for a manual pick."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    return {
        "aliases": call_agent_identity_aliases.list_aliases(org_id),
        "unresolved": call_agent_identity_aliases.suggest_identity_matches(
            org_id, _JUSTCALL_PROVIDER,
        ),
        "members": ticket_agent_aliases.list_org_agents(org_id),
    }


def set_call_agent_identity_alias(request: Request, body: SetCallIdentityAliasBody):
    """Owner-only: create or update this org's mapping for one
    provider+identifier. Backfills every already-ingested, still-
    unresolved call for that exact identifier in the same transaction."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    result = call_agent_identity_aliases.set_alias(
        org_id, body.provider, body.identifier, body.user_id,
    )
    applog.event(
        log, "call_agent_identity_alias_set_via_api",
        org_id=org_id, provider=result["provider"], identifier=result["identifier"],
    )
    return result


def delete_call_agent_identity_alias(request: Request, provider: str, identifier: str):
    """Owner-only: remove a mapping. Future ingestions of that identifier
    go back to unresolved; already-resolved calls are untouched."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    prov = (provider or "").strip()
    ident = (identifier or "").strip()
    if not prov or not ident:
        raise HTTPException(status_code=400, detail="provider and identifier are required.")
    call_agent_identity_aliases.delete_alias(org_id, prov, ident)
    return {"ok": True}


def register(app) -> None:
    app.add_api_route(
        "/api/calls/agent-identity-aliases", list_call_agent_identity_aliases, methods=["GET"],
    )
    app.add_api_route(
        "/api/calls/agent-identity-aliases", set_call_agent_identity_alias, methods=["POST"],
    )
    app.add_api_route(
        "/api/calls/agent-identity-aliases/{provider}/{identifier}",
        delete_call_agent_identity_alias, methods=["DELETE"],
    )
