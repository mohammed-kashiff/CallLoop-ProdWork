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
