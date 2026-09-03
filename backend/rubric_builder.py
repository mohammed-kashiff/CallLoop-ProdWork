"""Self-serve rubric builder: a team defines its own audit criteria — a mix
of CallLoop's built-in checks (reused unchanged, at a team-chosen weight)
and free-text custom criteria Claude judges.

Deliberately separate from admin_console.py: that module is Command
Center's platform-admin-only reweighting tool (unchanged, kept as-is), this
one is customer-facing and gated by auth.require_owner, not
require_platform_admin.
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
from . import audit_store
from . import db
from .org_ids import org_scope, parse_org_id
from .qa_v8 import RESOLUTION_QUESTION, TONE_QUESTION, list_dimensions

log = logging.getLogger("callproof.rubric_builder")

_WEIGHT_SUM = 100
_MAX_DIMENSIONS = 12
_MAX_QUESTION_LEN = 2000
_MAX_NAME_LEN = 80

# Plain-English starting text for "customize this criterion" on a built-in
# row (frontend). Editing it converts that dimension to a custom, Claude-
# judged one — the deterministic ones have no free-text criteria to begin
# with, so this is a reasonable, honestly-described starting point, not the
# literal logic that runs today.
_BUILTIN_DEFAULT_QUESTIONS = {
    "resolution_effectiveness": RESOLUTION_QUESTION,
    "tone_empathy_professionalism": TONE_QUESTION,
    "ownership_next_steps": (
        "Did the agent clearly state who owns the next step and when the "
        "customer should expect to hear back?"
    ),
    "active_listening": (
        "Did the agent let the customer finish speaking without interrupting, "
        "and avoid unexplained silence on the call?"
    ),
}


def _json_value(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _builtin_lookup() -> dict[str, dict]:
    return {d["id"]: d for d in list_dimensions(audit_store.load_v8_definition())}


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
    seen_ids: set[str] = set()
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
            dim = {
                "id": did, "name": name, "method": "custom_llm",
                "weight": weight, "question": question,
            }
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
    """The two-bucket shape qa_v8.list_dimensions()/is_v8_rubric() expect.
    bucket_weight is unused anywhere in scoring (display-only historically)
    so every dimension lives under one bucket for a self-serve rubric."""
    return {
        "technical_skills": {"bucket_weight": 0, "dimensions": dimensions},
        "soft_skills": {"bucket_weight": 0, "dimensions": []},
    }


def _available_builtins() -> list[dict]:
    return [
        {
            "id": d["id"], "name": d["name"],
            "default_question": _BUILTIN_DEFAULT_QUESTIONS.get(d["id"], ""),
        }
        for d in list_dimensions(audit_store.load_v8_definition())
    ]


def _describe(definition: dict) -> list[dict]:
    builtins = _builtin_lookup()
    out = []
    for dim in list_dimensions(definition):
        did = dim.get("id")
        if did in builtins:
            out.append({"kind": "builtin", "id": did, "name": dim.get("name"), "weight": dim.get("weight")})
        else:
            out.append({
                "kind": "custom", "id": did, "name": dim.get("name"),
                "weight": dim.get("weight"), "question": dim.get("question"),
            })
    return out


def current_rubric(org_id: str | None) -> dict:
    """The org's active rubric, described as builtin/custom dimension picks
    a UI can render directly. Falls back to CallLoop's default 4 dimensions,
    all marked "builtin", when the org hasn't customized anything yet."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, version, definition, updated_at
                FROM rubrics WHERE org_id = %s AND is_active LIMIT 1
                """,
                (oid,),
            ).fetchone()
    if row:
        definition = audit_store.decode_findings(row.get("definition"))
    else:
        definition = None
    if isinstance(definition, dict):
        source = "custom"
        rubric_id, name, version = str(row["id"]), str(row["name"]), int(row["version"])
        updated_at = _json_value(row.get("updated_at"))
    else:
        definition = audit_store.load_v8_definition()
        source, rubric_id, name, version, updated_at = "legacy", None, None, None, None
    return {
        "org_id": oid,
        "source": source,
        "rubric_id": rubric_id,
        "name": name,
        "version": version,
        "updated_at": updated_at,
        "dimensions": _describe(definition),
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
    row = conn.execute(
        "SELECT name FROM rubrics WHERE org_id = %s AND is_active LIMIT 1",
        (org_id,),
    ).fetchone()
    return (str(row["name"]) if row and row.get("name") else "") or audit_store.LEGACY_RUBRIC_NAME


def _save_response(org_id: str, saved: dict) -> dict:
    return {
        "org_id": org_id,
        "source": "custom",
        "rubric_id": saved["rubric_id"],
        "name": saved["name"],
        "version": saved["version"],
        "is_active": saved.get("is_active", True),
        "updated_at": _json_value(saved.get("updated_at")),
        "dimensions": _describe(saved["definition"]),
        "available_builtins": _available_builtins(),
    }


def save_rubric(
    org_id: str | None, raw_dimensions, *, changed_by: str,
    name: str | None = None, activate: bool = True,
) -> dict:
    """Save a fully self-serve rubric: any mix of built-in and custom
    dimensions the team picked, weights summing to 100. Never mutates an
    existing rubrics row — inserts a new version under this name and
    (if activate) deactivates whatever else was active for the org.

    name=None reuses whatever's currently active (or the legacy default
    name for a first-ever save) — the original single-rubric behavior,
    still what the plain "save my rubric" flow uses. Pass an explicit name
    to save a distinct, independently addressable library entry instead.
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
                conn, org_id=oid, name=resolved_name, definition=definition, activate=activate,
            )
    applog.event(
        log, "rubric_dimensions_saved",
        org_id=oid,
        rubric_id=saved["rubric_id"],
        rubric_name=saved["name"],
        version=saved["version"],
        activated=activate,
        changed_by=actor,
        dimension_ids=[d["id"] for d in dimensions],
    )
    return _save_response(oid, saved)


def list_rubrics(org_id: str | None) -> dict:
    """Every named rubric this org has saved — the library view."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    with org_scope(oid):
        with db.connection() as conn:
            lineages = audit_store.list_rubric_lineages(conn, org_id=oid)
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
    """One named rubric's latest version, described for the editor."""
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required.")
    name = _validate_name(name)
    with org_scope(oid):
        with db.connection() as conn:
            found = audit_store.fetch_rubric_by_name(conn, org_id=oid, name=name)
    if not found:
        raise HTTPException(status_code=404, detail=f"No saved rubric named {name!r}.")
    return {
        "org_id": oid,
        "source": "custom",
        "rubric_id": found["rubric_id"],
        "name": found["name"],
        "version": found["version"],
        "is_active": found["is_active"],
        "updated_at": _json_value(found.get("updated_at")),
        "dimensions": _describe(found["definition"]),
        "available_builtins": _available_builtins(),
    }


def activate_rubric(org_id: str | None, name: str, *, changed_by: str) -> dict:
    """Switch which saved rubric scores calls going forward — no dimension
    change, just a swap of which name is active."""
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
                activated = audit_store.activate_rubric_by_name(conn, org_id=oid, name=name)
            except ValueError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
    applog.event(
        log, "rubric_activated",
        org_id=oid, rubric_name=name, rubric_id=activated["rubric_id"],
        version=activated["version"], changed_by=actor,
    )
    return _save_response(oid, activated)
