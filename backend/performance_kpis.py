"""Assignable KPI targets for Team Performance.

Org-default (agent_user_id NULL) and per-agent overrides, keyed to the
live call/ticket rubric so a newly added custom criterion shows up as
an unset target without inserting a row. No Redis, no job, no Claude.
"""

from __future__ import annotations

import re
import uuid
from decimal import Decimal

from fastapi import HTTPException

from . import audit_log
from . import audit_store
from . import db
from . import qa_v8
from . import ticket_rubric
from .org_ids import org_scope, parse_org_id

OVERALL_ID = "__overall__"
_CHANNELS = frozenset({"ticket", "call"})
_DIM_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_SKIP_DIMS = frozenset({"response_timeliness"})


def _num_target(value: object) -> float:
    if isinstance(value, Decimal):
        return float(value)
    return round(float(value), 2)


def parse_channel(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in _CHANNELS:
        raise HTTPException(status_code=400, detail="channel must be ticket or call")
    return value


def parse_dimension_id(raw: object) -> str:
    value = str(raw or "").strip()
    if value == OVERALL_ID:
        return value
    if not _DIM_RE.fullmatch(value) or value in _SKIP_DIMS:
        raise HTTPException(status_code=400, detail="dimension_id is not a valid criterion")
    return value


def parse_target(raw: object) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        n = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="target must be a number from 0 to 100") from None
    if n < 0 or n > 100:
        raise HTTPException(status_code=400, detail="target must be a number from 0 to 100")
    return round(n, 2)


def _ticket_dimensions(org_id: str) -> list[dict]:
    rubric = ticket_rubric.fetch_active_ticket_rubric(org_id)
    dims = (rubric or {}).get("dimensions") or ticket_rubric.get_default_ticket_rubric()
    out = []
    seen: set[str] = set()
    for d in dims:
        if not isinstance(d, dict):
            continue
        dim_id = str(d.get("id") or "").strip()
        if not dim_id or dim_id in _SKIP_DIMS or dim_id in seen:
            continue
        seen.add(dim_id)
        out.append({"id": dim_id, "name": str(d.get("name") or dim_id).strip() or dim_id})
    return out


def _call_dimensions(org_id: str) -> list[dict]:
    with org_scope(org_id):
        with db.connection() as conn:
            _rid, _ver, definition = audit_store.fetch_active_rubric(conn, org_id=org_id)
    out = []
    seen: set[str] = set()
    for d in qa_v8.list_dimensions(definition or {}):
        dim_id = str(d.get("id") or "").strip()
        if not dim_id or dim_id in _SKIP_DIMS or dim_id in seen:
            continue
        seen.add(dim_id)
        out.append({"id": dim_id, "name": str(d.get("name") or dim_id).strip() or dim_id})
    return out


def rubric_dimensions(org_id: str, channel: str) -> list[dict]:
    if channel == "ticket":
        return _ticket_dimensions(org_id)
    return _call_dimensions(org_id)


def allowed_dimension_ids(org_id: str, channel: str) -> set[str]:
    return {OVERALL_ID} | {d["id"] for d in rubric_dimensions(org_id, channel)}


def fetch_stored(conn, org_id: str, *, only_agent_id: str | None = None) -> list[dict]:
    sql = """
        SELECT channel, dimension_id, agent_user_id, target
        FROM performance_kpis
        WHERE org_id = %s
    """
    params: list = [org_id]
    if only_agent_id:
        sql += " AND (agent_user_id IS NULL OR agent_user_id = %s)"
        params.append(only_agent_id)
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        channel = str(r.get("channel") or "").strip()
        dim_id = str(r.get("dimension_id") or "").strip()
        if channel not in _CHANNELS or not dim_id:
            continue
        out.append({
            "channel": channel,
            "dimension_id": dim_id,
            "agent_user_id": parse_org_id(r.get("agent_user_id")),
            "target": _num_target(r.get("target")),
        })
    return out


def _display_name(first: object, last: object) -> str:
    n = " ".join(p for p in (first, last) if isinstance(p, str) and p.strip()).strip()
    return n or "Unnamed teammate"


def _members(conn, org_id: str, *, only_user_id: str | None) -> list[dict]:
    sql = """
        SELECT user_id, first_name, last_name
        FROM org_members
        WHERE org_id = %s
    """
    params: list = [org_id]
    if only_user_id:
        sql += " AND user_id = %s"
        params.append(only_user_id)
    sql += " ORDER BY last_name NULLS LAST, first_name NULLS LAST"
    rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        uid = parse_org_id(r.get("user_id"))
        if not uid:
            continue
        out.append({
            "user_id": uid,
            "display_name": _display_name(r.get("first_name"), r.get("last_name")),
        })
    return out


def _is_org_member(conn, org_id: str, user_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM org_members WHERE org_id = %s AND user_id = %s",
        [org_id, user_id],
    ).fetchone()
    return bool(row)


def effective_target(rows: list[dict], channel: str, dim_id: str, agent_user_id: str | None) -> float | None:
    org_default = None
    override = None
    for r in rows:
        if r["channel"] != channel or r["dimension_id"] != dim_id:
            continue
        if r["agent_user_id"] is None:
            org_default = r["target"]
        elif agent_user_id and r["agent_user_id"] == agent_user_id:
            override = r["target"]
    if override is not None:
        return override
    return org_default


def met(actual: float | None, target: float | None) -> bool | None:
    if actual is None or target is None:
        return None
    return actual >= target


def apply_to_heatmap(grid: dict, channel: str, rows: list[dict]) -> dict:
    """Attach effective target/met onto each heatmap cell (mutates cells)."""
    for row in grid.get("rows") or []:
        uid = parse_org_id(row.get("user_id"))
        for cell in row.get("cells") or []:
            dim_id = str(cell.get("id") or "")
            target = effective_target(rows, channel, dim_id, uid)
            rate = cell.get("rate")
            actual = None if rate is None else round(float(rate) * 100, 2)
            cell["target"] = target
            cell["met"] = met(actual, target)
    return grid


def catalog(
    org_id: str,
    *,
    viewer_user_id: str,
    is_manager: bool,
) -> dict:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    if not oid or not vid:
        raise HTTPException(status_code=400, detail="org_id and viewer_user_id are required")
    only = None if is_manager else vid
    ticket_dims = rubric_dimensions(oid, "ticket")
    call_dims = rubric_dimensions(oid, "call")
    with org_scope(oid):
        with db.connection() as conn:
            stored = fetch_stored(conn, oid, only_agent_id=only)
            roster = _members(conn, oid, only_user_id=only)
    names = {m["user_id"]: m["display_name"] for m in roster}

    def _channel(channel: str, dims: list[dict]) -> dict:
        catalog_dims = [{"id": OVERALL_ID, "name": "Overall score"}] + dims
        out = []
        for dim in catalog_dims:
            org_target = effective_target(stored, channel, dim["id"], None)
            overrides = []
            for r in stored:
                if (
                    r["channel"] == channel
                    and r["dimension_id"] == dim["id"]
                    and r["agent_user_id"]
                ):
                    overrides.append({
                        "user_id": r["agent_user_id"],
                        "display_name": names.get(r["agent_user_id"]) or "Former teammate",
                        "target": r["target"],
                    })
            out.append({
                "id": dim["id"],
                "name": dim["name"],
                "org_target": org_target,
                "overrides": overrides,
            })
        return {"dimensions": out}

    return {
        "tickets": _channel("ticket", ticket_dims),
        "calls": _channel("call", call_dims),
        "roster": [
            {"user_id": m["user_id"], "display_name": m["display_name"]}
            for m in roster
        ] if is_manager else [],
    }


def upsert(
    org_id: str,
    *,
    channel: str,
    dimension_id: str,
    agent_user_id: str | None,
    target: float | None,
) -> dict:
    oid = parse_org_id(org_id)
    if not oid:
        raise HTTPException(status_code=400, detail="org_id is required")
    ch = parse_channel(channel)
    dim = parse_dimension_id(dimension_id)
    if dim not in allowed_dimension_ids(oid, ch):
        raise HTTPException(status_code=400, detail="dimension_id is not on the active rubric")
    agent = parse_org_id(agent_user_id) if agent_user_id else None
    if agent_user_id and not agent:
        raise HTTPException(status_code=400, detail="Unknown teammate.")
    parsed_target = parse_target(target) if target is not None else None
    # Distinguish omitted vs explicit null: callers pass None to clear.
    if target is None:
        parsed_target = None

    before = None
    after = None
    with org_scope(oid):
        with db.connection() as conn:
            if agent and not _is_org_member(conn, oid, agent):
                raise HTTPException(status_code=400, detail="Unknown teammate.")
            existing = conn.execute(
                """
                SELECT target FROM performance_kpis
                WHERE org_id = %s AND channel = %s AND dimension_id = %s
                  AND agent_user_id IS NOT DISTINCT FROM %s
                """,
                [oid, ch, dim, agent],
            ).fetchone()
            if existing:
                before = {"target": _num_target(existing.get("target"))}
            if parsed_target is None:
                conn.execute(
                    """
                    DELETE FROM performance_kpis
                    WHERE org_id = %s AND channel = %s AND dimension_id = %s
                      AND agent_user_id IS NOT DISTINCT FROM %s
                    """,
                    [oid, ch, dim, agent],
                )
            elif existing:
                conn.execute(
                    """
                    UPDATE performance_kpis
                    SET target = %s, updated_at = now()
                    WHERE org_id = %s AND channel = %s AND dimension_id = %s
                      AND agent_user_id IS NOT DISTINCT FROM %s
                    """,
                    [parsed_target, oid, ch, dim, agent],
                )
                after = {"target": parsed_target}
            else:
                conn.execute(
                    """
                    INSERT INTO performance_kpis
                        (id, org_id, channel, dimension_id, agent_user_id, target)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [str(uuid.uuid4()), oid, ch, dim, agent, parsed_target],
                )
                after = {"target": parsed_target}

    action = "kpi.target_cleared" if parsed_target is None else "kpi.target_set"
    audit_log.record(
        oid,
        action,
        target_type="performance_kpi",
        target_id=f"{ch}:{dim}:{agent or 'org'}",
        before=before,
        after=after,
    )
    return {
        "channel": ch,
        "dimension_id": dim,
        "agent_user_id": agent,
        "target": parsed_target,
    }
