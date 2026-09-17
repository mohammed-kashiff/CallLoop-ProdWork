"""Training drills from stored Top Gap findings.

No new Claude call, job, or table. Ticket drills use finding reasoning;
call drills prefer coaching_note. Org-scoped SQL with RLS. Members always
see themselves; Owner/Manager may pick an org teammate.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from . import db
from . import team_performance
from .org_ids import org_scope, parse_org_id

_DRILL_LIMIT = 5
_DIM_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CHANNELS = frozenset({"ticket", "call"})


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def parse_channel(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    value = str(raw).strip().lower()
    if value not in _CHANNELS:
        raise ValueError("channel must be ticket or call")
    return value


def parse_dim(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    value = str(raw).strip()
    if not _DIM_RE.fullmatch(value):
        raise ValueError("dim is not a valid dimension id")
    return value


def _focus_name(dim_id: str, channel: str, drills: list[dict]) -> str:
    for row in drills:
        name = str(row.get("dimension_name") or "").strip()
        if name:
            return name
    catalog = team_performance._TICKET_DIMS if channel == "ticket" else team_performance._CALL_DIMS
    for cid, cname in catalog:
        if cid == dim_id:
            return cname
    return dim_id


def _highlight_brief(hit: dict | None) -> dict | None:
    if not hit:
        return None
    dim_id = str(hit.get("id") or "").strip()
    name = str(hit.get("name") or "").strip() or dim_id
    if not dim_id:
        return None
    return {"id": dim_id, "name": name}


def _ticket_drills(
    conn, org_id: str, days: int, agent_user_id: str, dim_id: str,
) -> list[dict]:
    findings = team_performance._FINDINGS_ARRAY_SQL.format(alias="ta")
    sql = f"""
        SELECT ta.ticket_id,
               ta.created_at,
               f->>'id' AS dim_id,
               COALESCE(NULLIF(f->>'name', ''), f->>'id') AS dim_name,
               f->>'reasoning' AS reasoning,
               f->>'evidence_text' AS evidence_text,
               f->>'coaching_note' AS coaching_note
        FROM ticket_audits ta
        CROSS JOIN LATERAL jsonb_array_elements({findings}) AS f
        WHERE ta.org_id = %s
          AND ta.agent_user_id = %s
          AND ta.created_at >= now() - (%s * INTERVAL '1 day')
          AND f->>'id' = %s
          AND f->>'verdict' = 'fail'
        ORDER BY ta.created_at DESC
        LIMIT %s
    """
    rows = conn.execute(sql, [org_id, agent_user_id, days, dim_id, _DRILL_LIMIT]).fetchall()
    out = []
    for r in rows:
        ticket_id = parse_org_id(r.get("ticket_id"))
        if not ticket_id:
            continue
        out.append({
            "channel": "ticket",
            "ticket_id": ticket_id,
            "call_id": None,
            "dimension_id": str(r.get("dim_id") or dim_id),
            "dimension_name": str(r.get("dim_name") or dim_id),
            "reasoning": (str(r.get("reasoning") or "").strip() or None),
            "evidence_text": (str(r.get("evidence_text") or "").strip() or None),
            "coaching_note": (str(r.get("coaching_note") or "").strip() or None),
            "created_at": _iso(r.get("created_at")),
        })
    return out


def _call_drills(
    conn, org_id: str, days: int, agent_user_id: str, dim_id: str,
) -> list[dict]:
    findings = team_performance._FINDINGS_ARRAY_SQL.format(alias="latest")
    sql = f"""
        WITH latest AS (
            SELECT DISTINCT ON (call_id) call_id, org_id, findings, created_at
            FROM audits
            WHERE org_id = %s AND score IS NOT NULL
            ORDER BY call_id, created_at DESC
        )
        SELECT c.id AS call_id,
               latest.created_at,
               f->>'id' AS dim_id,
               COALESCE(NULLIF(f->>'name', ''), f->>'id') AS dim_name,
               f->>'reasoning' AS reasoning,
               f->>'evidence_text' AS evidence_text,
               f->>'coaching_note' AS coaching_note
        FROM calls c
        INNER JOIN latest ON latest.call_id = c.id AND latest.org_id = c.org_id
        CROSS JOIN LATERAL jsonb_array_elements({findings}) AS f
        WHERE c.org_id = %s
          AND c.deleted_at IS NULL
          AND c.agent_user_id = %s
          AND latest.created_at >= now() - (%s * INTERVAL '1 day')
          AND f->>'id' = %s
          AND f->>'verdict' = 'fail'
        ORDER BY latest.created_at DESC
        LIMIT %s
    """
    rows = conn.execute(
        sql, [org_id, org_id, agent_user_id, days, dim_id, _DRILL_LIMIT],
    ).fetchall()
    out = []
    for r in rows:
        call_id = r.get("call_id")
        try:
            cid = int(call_id)
        except (TypeError, ValueError):
            continue
        if cid < 1:
            continue
        out.append({
            "channel": "call",
            "ticket_id": None,
            "call_id": cid,
            "dimension_id": str(r.get("dim_id") or dim_id),
            "dimension_name": str(r.get("dim_name") or dim_id),
            "reasoning": (str(r.get("reasoning") or "").strip() or None),
            "evidence_text": (str(r.get("evidence_text") or "").strip() or None),
            "coaching_note": (str(r.get("coaching_note") or "").strip() or None),
            "created_at": _iso(r.get("created_at")),
        })
    return out


def snapshot(
    org_id: str,
    *,
    viewer_user_id: str,
    is_manager: bool,
    days: int,
    channel: str | None = None,
    dim: str | None = None,
    agent: str | None = None,
) -> dict:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    if not oid or not vid:
        raise ValueError("org_id and viewer_user_id are required")
    n = team_performance.clamp_days(days)
    requested_agent = parse_org_id(agent) if agent else None
    if agent and not requested_agent:
        raise ValueError("Unknown teammate.")
    if not is_manager:
        target = vid
    else:
        target = requested_agent or vid

    with org_scope(oid):
        with db.connection() as conn:
            roster = team_performance._members(conn, oid, only_user_id=None)
            by_id = {m["user_id"]: m for m in roster}
            if target not in by_id:
                raise ValueError("Unknown teammate.")
            ticket_h = team_performance._ticket_highlights(
                conn, oid, n, only_user_id=target,
            ).get(target) or {}
            call_h = team_performance._call_highlights(
                conn, oid, n, only_user_id=target,
            ).get(target) or {}
            ticket_gap = _highlight_brief(ticket_h.get("top_gap"))
            call_gap = _highlight_brief(call_h.get("top_gap"))

            ticket_focus = None
            call_focus = None
            if dim and channel == "ticket":
                ticket_focus = {"id": dim, "name": _focus_name(dim, "ticket", [])}
            elif dim and channel == "call":
                call_focus = {"id": dim, "name": _focus_name(dim, "call", [])}
            elif dim:
                ticket_focus = {"id": dim, "name": _focus_name(dim, "ticket", [])}
                call_focus = {"id": dim, "name": _focus_name(dim, "call", [])}
            else:
                if channel != "call":
                    ticket_focus = ticket_gap
                if channel != "ticket":
                    call_focus = call_gap

            drills: list[dict] = []
            if ticket_focus:
                ticket_drills = _ticket_drills(conn, oid, n, target, ticket_focus["id"])
                drills.extend(ticket_drills)
                ticket_focus = {
                    "id": ticket_focus["id"],
                    "name": _focus_name(ticket_focus["id"], "ticket", ticket_drills),
                }
            if call_focus:
                call_drills = _call_drills(conn, oid, n, target, call_focus["id"])
                drills.extend(call_drills)
                call_focus = {
                    "id": call_focus["id"],
                    "name": _focus_name(call_focus["id"], "call", call_drills),
                }

    member = by_id[target]
    return {
        "view_scope": "team" if is_manager else "own",
        "days": n,
        "agent": {
            "user_id": member["user_id"],
            "display_name": member["display_name"],
            "role": member["role"],
        },
        "roster": [
            {"user_id": m["user_id"], "display_name": m["display_name"]}
            for m in roster
        ] if is_manager else [],
        "focus": {
            "ticket": ticket_focus,
            "call": call_focus,
            "channel": channel,
            "dim": dim,
        },
        "drills": drills,
    }
