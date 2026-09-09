"""
CallProof - Intercom REST client (IN-5/IN-7).

Thin wrapper over the endpoints this integration actually calls, same role
justcall.py plays for JustCall. Uses the per-org access token from the
vault (org_vault.load_credential(org_id, "intercom")) — never a host-level
key, since this is multi-tenant.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("callproof.intercom")

BASE_URL = "https://api.intercom.io"


def _headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "Intercom-Version": "2.14",
        "Accept": "application/json",
    }


def get_conversation(access_token: str, conversation_id: str) -> dict:
    """GET /conversations/{id} — full object, including conversation_parts
    and the initiating `source` message. Raises httpx.HTTPStatusError on
    a non-2xx response (a 404 here means the id in a webhook/event doesn't
    exist or isn't visible to this token — a real error, not a soft case)."""
    cid = str(conversation_id).strip()
    if not cid:
        raise ValueError("conversation_id is required")
    r = httpx.get(
        f"{BASE_URL}/conversations/{cid}",
        headers=_headers(access_token),
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def get_ticket(access_token: str, ticket_id: str) -> dict:
    """GET /tickets/{id} — full object, including ticket_attributes
    (opening description), contacts (requester), and ticket_parts (the
    reply/note thread). Raises httpx.HTTPStatusError on a non-2xx
    response, same contract as get_conversation."""
    tid = str(ticket_id).strip()
    if not tid:
        raise ValueError("ticket_id is required")
    r = httpx.get(
        f"{BASE_URL}/tickets/{tid}",
        headers=_headers(access_token),
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json() if r.content else {}


def search_closed_conversations(
    access_token: str, since_unix: int, *, per_page: int = 50,
) -> list[dict]:
    """POST /conversations/search — conversations closed since `since_unix`
    (Unix seconds). This is the actual filterable endpoint: plain
    GET /conversations (List) supports no state or date filtering at all,
    only cursor pagination — using it for "what closed recently" would
    mean paginating the org's entire conversation history every poll
    cycle. Confirmed against Intercom's own API reference (2026-09-09)
    before choosing this over the PRD's original suggestion of the List
    endpoint, which can't actually do this job.

    One page only (per_page, default 50) — this is a backstop for what a
    webhook missed, run frequently enough (see intercom_oauth.poll_seconds)
    that a single page comfortably covers the gap; it is not a full
    historical backfill mechanism.
    """
    body = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "state", "operator": "=", "value": "closed"},
                {"field": "updated_at", "operator": ">", "value": str(int(since_unix))},
            ],
        },
        "pagination": {"per_page": per_page},
    }
    r = httpx.post(
        f"{BASE_URL}/conversations/search",
        headers=_headers(access_token),
        json=body,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    conversations = data.get("conversations") or []
    return [c for c in conversations if isinstance(c, dict)]


def search_closed_tickets(
    access_token: str, since_unix: int, *, per_page: int = 50,
) -> list[dict]:
    """POST /tickets/search — tickets closed since `since_unix` (Unix
    seconds).

    Filters on `open = false`, not a `state` string value. Tickets don't
    share Conversations' state vocabulary ("open"/"closed"/"snoozed") —
    a ticket's lifecycle is tracked via `ticket_state.category` (type-
    specific stages like "submitted"/"in_progress"/"resolved") plus a
    separate top-level `open` boolean that only flips to false on actual
    closure. Confirmed live: a real ticket that went through "Resolved"
    then "closed" in Intercom's UI never once matched `state = "closed"`
    across 12+ poll cycles (empty results, not an error) — `open` is
    the correct, type-agnostic closed signal.
    """
    body = {
        "query": {
            "operator": "AND",
            "value": [
                {"field": "open", "operator": "=", "value": False},
                {"field": "updated_at", "operator": ">", "value": str(int(since_unix))},
            ],
        },
        "pagination": {"per_page": per_page},
    }
    r = httpx.post(
        f"{BASE_URL}/tickets/search",
        headers=_headers(access_token),
        json=body,
        timeout=30.0,
    )
    r.raise_for_status()
    data = r.json() if r.content else {}
    tickets = data.get("tickets") or []
    return [t for t in tickets if isinstance(t, dict)]
