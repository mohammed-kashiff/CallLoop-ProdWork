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
