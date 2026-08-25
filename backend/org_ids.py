"""Tenant ids. Application-layer isolation (CL-9) is independent of RLS.

org_id for HTTP work comes from the verified JWT membership on request.state,
never from a query param, path, or JSON body. Background jobs (JustCall webhook
/ poller, CLI) bind an explicit org — env JUSTCALL_ORG_ID or DEFAULT_ORG_ID —
never a payload field named org_id.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from contextvars import ContextVar, Token

# UUID v4-shaped, stable across environments. Seeded in Alembic revision 0001.
DEFAULT_ORG_ID = "00000000-0000-4000-8000-000000000001"

# Legacy v8 rubric for the placeholder org. Seeded in Alembic revision 0003.
# Other orgs get their own rubric UUID at seed time — never reuse this id.
DEFAULT_RUBRIC_ID = "00000000-0000-4000-8000-000000000011"

_ORG_ID: ContextVar[str | None] = ContextVar("callproof_org_id", default=None)


def parse_org_id(value: object) -> str | None:
    """Return a UUID string or None. Does not read HTTP input."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return str(uuid.UUID(raw))
    except (ValueError, TypeError, AttributeError):
        return None


def bound_org_id() -> str | None:
    """Org bound for this task (JWT middleware or org_scope)."""
    return _ORG_ID.get()


def bind_org_id(org_id: str) -> Token:
    parsed = parse_org_id(org_id)
    if not parsed:
        raise ValueError("org_id must be a UUID")
    return _ORG_ID.set(parsed)


def reset_org_id(token: Token) -> None:
    _ORG_ID.reset(token)


@contextmanager
def org_scope(org_id: str):
    """Bind org_id for the current task (request thread or worker)."""
    token = bind_org_id(org_id)
    try:
        yield parse_org_id(org_id)
    finally:
        reset_org_id(token)


def integration_org_id() -> str:
    """Org for unauthenticated ingest (JustCall webhook/poller). Never from payload."""
    parsed = parse_org_id(os.getenv("JUSTCALL_ORG_ID"))
    return parsed or DEFAULT_ORG_ID
