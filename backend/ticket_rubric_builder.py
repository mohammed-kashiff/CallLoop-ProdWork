"""Self-serve TICKET rubric builder (TA-24 follow-on): the exact
customer-facing, owner-gated mechanism rubric_builder.py already proved
out for call rubrics — mix of built-in and free-text custom dimensions,
weights summing to 100, saved under a name, multiple named rubrics
coexisting, choose-which-is-active — now for kind="ticket" rows in the
same `rubrics` table (PRD §10, no schema change).

Deliberately a separate module, not folded into rubric_builder.py: the
builtin dimension set (ticket_rubric.TICKET_QA_DIMENSIONS, not qa_v8's
call dimensions), the wire shape (a flat dimensions list — what
ticket_scoring.score_ticket() reads — not the two-bucket call shape),
and the reserved "response_timeliness" id are all ticket-specific.
Reuses audit_store.py's kind= parameter on save_named_rubric/
list_rubric_lineages/fetch_rubric_by_name/activate_rubric_by_name rather
than a second copy of the cross-engine isolation logic TA-35 (and the
matching activate_rubric_by_name bug fixed alongside this module) had to
get right for the call side.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from . import applog
from . import audit_log
from . import audit_store
from . import db
from . import product_events
from . import ticket_rubric
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.ticket_rubric_builder")

_KIND = "ticket"
_WEIGHT_SUM = 100
_MAX_DIMENSIONS = 12
_MAX_QUESTION_LEN = 2000
_MAX_NAME_LEN = 80

# response_timeliness is always computed deterministically and appended
# outside the weighted score (ticket_score_api._with_timeliness) — never a
# pickable, weighted dimension a team could shadow or double up on.
_RESERVED_IDS = {"response_timeliness"}


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _builtin_lookup() -> dict[str, dict]:
    return {d["id"]: d for d in ticket_rubric.TICKET_QA_DIMENSIONS}


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or f"criterion_{uuid.uuid4().hex[:8]}"


def _normalize_dimensions(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="At least one dimension is required.")
    if len(raw) > _MAX_DIMENSIONS:
        raise HTTPException(
            status_code=400, detail=f"No more than {_MAX_DIMENSIONS} dimensions.",
        )
    builtins = _builtin_lookup()
    out: list[dict] = []
    seen_ids: set[str] = set(_RESERVED_IDS)
    total = 0
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Each dimension must be an object.")
        weight = item.get("weight")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or weight < 0
            or int(weight) != weight
        ):
            raise HTTPException(
                status_code=400, detail="Each dimension needs a whole-number weight.",
            )
        weight = int(weight)
        kind = item.get("kind")
        if kind == "builtin":
            bid = item.get("id")
            if bid not in builtins:
                raise HTTPException(
                    status_code=400, detail=f"Unknown built-in dimension: {bid}",
                )
            dim = dict(builtins[bid])
            dim["weight"] = weight
        elif kind == "custom":
            name = (item.get("name") or "").strip()
            question = (item.get("question") or "").strip()
            if not name or not question:
                raise HTTPException(
                    status_code=400,
                    detail="Custom dimensions need a name and criteria text.",
                )
            if len(question) > _MAX_QUESTION_LEN:
                raise HTTPException(
                    status_code=400,
                    detail=f"Criteria text is too long ({_MAX_QUESTION_LEN} characters max).",
                )
            base_id = _slugify(name)
            did = base_id
            n = 2
            while did in seen_ids:
                did = f"{base_id}_{n}"
                n += 1
            dim = {"id": did, "name": name, "weight": weight, "question": question}
            if item.get("customer_facing_only"):
                dim["customer_facing_only"] = True
        else:
            raise HTTPException(
                status_code=400, detail="Each dimension needs kind 'builtin' or 'custom'.",
            )
        if dim["id"] in seen_ids:
            raise HTTPException(status_code=400, detail=f"Duplicate dimension id: {dim['id']}")
        seen_ids.add(dim["id"])
        total += weight
        out.append(dim)
    if total != _WEIGHT_SUM:
        raise HTTPException(status_code=400, detail="Weights must sum to 100.")
    return out


def _wrap_definition(dimensions: list[dict]) -> dict[str, Any]:
    """ticket_rubric/ticket_scoring's flat shape — never the two-bucket
    call shape, which nothing on the ticket read path understands."""
    return {"kind": _KIND, "dimensions": dimensions}


def _available_builtins() -> list[dict]:
    return [
        {
            "id": d["id"], "name": d["name"], "default_question": d["question"],
            "customer_facing_only": bool(d.get("customer_facing_only")),
        }
        for d in ticket_rubric.TICKET_QA_DIMENSIONS
    ]


def _describe(dimensions: list[dict] | None) -> list[dict]:
    builtins = _builtin_lookup()
    out = []
    for dim in dimensions or []:
        did = dim.get("id")
        if did in builtins:
            out.append({
                "kind": "builtin", "id": did, "name": dim.get("name"),
                "weight": dim.get("weight"),
                "customer_facing_only": bool(dim.get("customer_facing_only")),
            })
        else:
            out.append({
                "kind": "custom", "id": did, "name": dim.get("name"),
                "weight": dim.get("weight"), "question": dim.get("question"),
                "customer_facing_only": bool(dim.get("customer_facing_only")),
            })
    return out


def current_rubric(org_id: str | None) -> dict:
    """The org's active Ticket QA rubric, described for the builder editor
    the same builtin/custom shape rubric_builder.current_rubric() uses.
    Always a real row — ensure_ticket_rubric() seeds TA-13's default the
    first time an org opens this builder or scores a ticket, whichever
    comes first."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    rubric = ticket_rubric.ensure_ticket_rubric(oid)
    return {
        "org_id": oid,
        "rubric_id": rubric.get("id"),
        "name": rubric.get("name") or ticket_rubric.TICKET_QA_RUBRIC_NAME,
        "version": rubric.get("version"),
        "updated_at": _json_value(rubric.get("updated_at")),
        "dimensions": _describe(rubric.get("dimensions")),
        "available_builtins": _available_builtins(),
    }


def _validate_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required.")
    if len(name) > _MAX_NAME_LEN:
        raise HTTPException(status_code=400, detail=f"name is too long ({_MAX_NAME_LEN} characters max).")
    return name


def _resolve_default_name(conn, org_id: str) -> str:
    """What name a plain "save my rubric" (no explicit name) writes under
    — scoped to kind="ticket" so this can never pick up an org's active
    CALL rubric's name."""
    row = conn.execute(
        """
        SELECT name FROM rubrics
        WHERE org_id = %s AND is_active
          AND COALESCE(definition->>'kind', 'call') = %s
        LIMIT 1
        """,
        (org_id, _KIND),
    ).fetchone()
    return (str(row["name"]) if row and row.get("name") else "") or ticket_rubric.TICKET_QA_RUBRIC_NAME


def _save_response(org_id: str, saved: dict) -> dict:
    definition = saved["definition"] if isinstance(saved["definition"], dict) else {}
    return {
        "org_id": org_id,
        "rubric_id": saved["rubric_id"],
        "name": saved["name"],
        "version": saved["version"],
        "is_active": saved.get("is_active", True),
        "updated_at": _json_value(saved.get("updated_at")),
        "dimensions": _describe(definition.get("dimensions")),
        "available_builtins": _available_builtins(),
    }


def save_rubric(
    org_id: str | None, raw_dimensions, *, changed_by: str,
    name: str | None = None, activate: bool = True,
) -> dict:
    """Save a fully self-serve ticket rubric: any mix of TICKET_QA_
    DIMENSIONS built-ins and custom dimensions the team picked, weights
    summing to 100. Never mutates an existing rubrics row — inserts a new
    version under this name and (if activate) deactivates whatever else
    was active for the org *within kind="ticket" only* (audit_store.
    save_named_rubric's kind= parameter — a call rubric save can never
    touch this row, and this save can never touch a call rubric).

    name=None reuses whatever's currently active (or "Ticket QA" for a
    first-ever named save) — same single-rubric-by-default behavior
    rubric_builder.save_rubric gives calls.
    """
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    actor = (changed_by or "").strip().lower()
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")
    dimensions = _normalize_dimensions(raw_dimensions)
    definition = _wrap_definition(dimensions)
    with org_scope(oid):
        with db.connection() as conn:
            resolved_name = _validate_name(name) if name else _resolve_default_name(conn, oid)
            saved = audit_store.save_named_rubric(
                conn, org_id=oid, name=resolved_name, definition=definition,
                activate=activate, kind=_KIND,
            )
    applog.event(
        log, "ticket_rubric_dimensions_saved",
        org_id=oid,
        rubric_id=saved["rubric_id"],
        rubric_name=saved["name"],
        version=saved["version"],
        activated=activate,
        changed_by=actor,
        dimension_ids=[d["id"] for d in dimensions],
    )
    audit_log.record(
        oid, "rubric.saved",
        target_type="rubric", target_id=saved["name"],
        after={"version": saved["version"], "activated": activate, "kind": "ticket"},
        actor_email=actor,
    )
    builtins = _builtin_lookup()
    has_custom = any(d["id"] not in builtins for d in dimensions)
    product_events.track_event(
        oid, None, "ticket_rubric_saved",
        {"kind": "custom" if has_custom else "reweight", "dimension_count": len(dimensions)},
    )
    return _save_response(oid, saved)


def list_rubrics(org_id: str | None) -> dict:
    """Every named ticket rubric this org has saved — the library view."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        with db.connection() as conn:
            lineages = audit_store.list_rubric_lineages(conn, org_id=oid, kind=_KIND)
    return {
        "org_id": oid,
        "rubrics": [
            {
                "rubric_id": r["rubric_id"],
                "name": r["name"],
                "version": r["version"],
                "is_active": r["is_active"],
                "updated_at": _json_value(r.get("updated_at")),
            }
            for r in lineages
        ],
    }


def get_rubric(org_id: str | None, name: str) -> dict:
    """One named ticket rubric's latest version, described for the editor."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    name = _validate_name(name)
    with org_scope(oid):
        with db.connection() as conn:
            found = audit_store.fetch_rubric_by_name(conn, org_id=oid, name=name, kind=_KIND)
    if not found:
        raise HTTPException(status_code=404, detail=f"No saved ticket rubric named {name!r}.")
    definition = found["definition"] if isinstance(found["definition"], dict) else {}
    return {
        "org_id": oid,
        "rubric_id": found["rubric_id"],
        "name": found["name"],
        "version": found["version"],
        "is_active": found["is_active"],
        "updated_at": _json_value(found.get("updated_at")),
        "dimensions": _describe(definition.get("dimensions")),
        "available_builtins": _available_builtins(),
    }


def activate_rubric(org_id: str | None, name: str, *, changed_by: str) -> dict:
    """Switch which saved ticket rubric scores tickets going forward — no
    dimension change, just a swap of which name is active (kind="ticket"
    only — can never deactivate or pick up an org's call rubric)."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    name = _validate_name(name)
    actor = (changed_by or "").strip().lower()
    if not actor or len(actor) > 254:
        raise HTTPException(status_code=400, detail="changed_by is required.")
    with org_scope(oid):
        with db.connection() as conn:
            try:
                activated = audit_store.activate_rubric_by_name(
                    conn, org_id=oid, name=name, kind=_KIND,
                )
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
    applog.event(
        log, "ticket_rubric_activated",
        org_id=oid, rubric_name=name, rubric_id=activated["rubric_id"],
        version=activated["version"], changed_by=actor,
    )
    audit_log.record(
        oid, "rubric.activated",
        target_type="rubric", target_id=name,
        after={"version": activated["version"], "kind": "ticket"},
        actor_email=actor,
    )
    return _save_response(oid, activated)
