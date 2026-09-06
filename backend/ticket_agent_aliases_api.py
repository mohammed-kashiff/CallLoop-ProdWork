"""TA-15 HTTP surface: org owner manages the mapping from a ticket PDF's
raw agent display name to a real org_members.user_id.

Separate module from ticket_api.py deliberately — that file is Cursor's
(TA-9); this keeps TA-15 from touching it while both are worked on the
same tree. Registered the same way (`register(app)`, add_api_route),
under /api/tickets/agent-aliases so it reads as part of the same
surface without living in the same file.

Every mutation is owner-only (auth.require_owner) — this is admin
configuration, same tier as the rubric builder and org_features toggles,
not something an individual agent account can change about themselves.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import auth
from . import ticket_agent_aliases

log = logging.getLogger("callproof.ticket_agent_aliases_api")


class SetAliasBody(BaseModel):
    display_name: str
    user_id: str


def list_agent_aliases(request: Request):
    """Owner-only: current mappings, unresolved raw names still needing
    one, and the org roster to pick a person from."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    return {
        "aliases": ticket_agent_aliases.list_aliases(org_id),
        "unresolved": ticket_agent_aliases.list_unresolved_agent_names(org_id),
        "members": ticket_agent_aliases.list_org_agents(org_id),
    }


def set_agent_alias(request: Request, body: SetAliasBody):
    """Owner-only: create or update this org's mapping for one display
    name. Applies to future ingestions only — does not retroactively
    backfill agent_user_id on tickets already ingested."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    result = ticket_agent_aliases.set_alias(org_id, body.display_name, body.user_id)
    applog.event(
        log, "ticket_agent_alias_set_via_api",
        org_id=org_id, display_name=result["display_name"],
    )
    return result


def delete_agent_alias(request: Request, display_name: str):
    """Owner-only: remove a mapping. Future ingestions of that name go
    back to unresolved; already-ingested turns are untouched."""
    auth.require_owner(request)
    org_id = auth.org_id_from_request(request)
    name = (display_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="display_name is required.")
    ticket_agent_aliases.delete_alias(org_id, name)
    return {"ok": True}


def register(app) -> None:
    app.add_api_route(
        "/api/tickets/agent-aliases", list_agent_aliases, methods=["GET"],
    )
    app.add_api_route(
        "/api/tickets/agent-aliases", set_agent_alias, methods=["POST"],
    )
    app.add_api_route(
        "/api/tickets/agent-aliases/{display_name}", delete_agent_alias, methods=["DELETE"],
    )
