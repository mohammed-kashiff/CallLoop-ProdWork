"""Team Performance Dashboard rollups.

Org-scoped, RLS-respecting SQL — no background job, no Redis. Ticket
scores come from ticket_audits (one row per agent, TA-28). Call scores
come from the latest audits row per call, joined to calls.agent_user_id
(IN-22). Ticket and call Top Strength/Gap are the mode of
ticket_audit_summary.top_strength/top_gap across that agent's stored
findings in the window (call findings share the same verdict/weight/name
shape).

Owner/Manager see every teammate. A member sees only their own row.
org_id is the JWT tenant; never a query param.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from typing import Any

from . import db
from . import ticket_audit_summary
from .org_ids import org_scope, parse_org_id

_DAYS_DEFAULT = 30
_DAYS_MAX = 365
_UNASSIGNED = "__unassigned__"


def clamp_days(raw: object) -> int:
    try:
        n = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DAYS_DEFAULT
    return max(1, min(n, _DAYS_MAX))


def _iso_week(value: object) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _num(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _display_name(first: object, last: object) -> str:
    n = " ".join(p for p in (first, last) if isinstance(p, str) and p.strip()).strip()
    return n or "Unnamed teammate"


def _findings_list(payload: object) -> list[dict]:
    data: Any = payload
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        items = data.get("findings")
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    return []


def _mode_highlight(rows: list[dict], picker) -> dict | None:
    counts: Counter[str] = Counter()
    examples: dict[str, dict] = {}
    for row in rows:
        hit = picker(_findings_list(row.get("findings")))
        if not hit:
            continue
        key = str(hit.get("id") or hit.get("name") or "").strip()
        if not key:
            continue
        counts[key] += 1
        examples[key] = {"id": hit.get("id"), "name": hit.get("name")}
    if not counts:
        return None
    winner = counts.most_common(1)[0][0]
    return examples[winner]


def _members(conn, org_id: str, *, only_user_id: str | None) -> list[dict]:
    sql = """
        SELECT user_id, role, first_name, last_name
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
            "role": (r.get("role") or "member").strip().lower() or "member",
            "display_name": _display_name(r.get("first_name"), r.get("last_name")),
        })
    return out


def _ticket_stats(conn, org_id: str, days: int, *, only_user_id: str | None) -> dict[str, dict]:
    sql = """
        SELECT agent_user_id, AVG(score) AS avg_score, COUNT(*)::int AS n
        FROM ticket_audits
        WHERE org_id = %s
          AND created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, days]
    if only_user_id:
        sql += " AND agent_user_id = %s"
        params.append(only_user_id)
    sql += " GROUP BY agent_user_id"
    rows = conn.execute(sql, params).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        uid = parse_org_id(r.get("agent_user_id"))
        if not uid:
            continue
        out[uid] = {"avg_score": _num(r.get("avg_score")), "count": _int(r.get("n"))}
    return out


def _call_stats(conn, org_id: str, days: int, *, only_user_id: str | None) -> dict[str, dict]:
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (call_id) call_id, org_id, score, created_at
            FROM audits
            WHERE org_id = %s AND score IS NOT NULL
            ORDER BY call_id, created_at DESC
        )
        SELECT c.agent_user_id, AVG(latest.score) AS avg_score, COUNT(*)::int AS n
        FROM calls c
        INNER JOIN latest ON latest.call_id = c.id AND latest.org_id = c.org_id
        WHERE c.org_id = %s
          AND c.deleted_at IS NULL
          AND latest.created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, org_id, days]
    if only_user_id:
        sql += " AND c.agent_user_id = %s"
        params.append(only_user_id)
    sql += " GROUP BY c.agent_user_id"
    rows = conn.execute(sql, params).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        uid = parse_org_id(r.get("agent_user_id")) or _UNASSIGNED
        out[uid] = {"avg_score": _num(r.get("avg_score")), "count": _int(r.get("n"))}
    if only_user_id:
        out.pop(_UNASSIGNED, None)
    return out


def _ticket_weeks(conn, org_id: str, days: int, *, only_user_id: str | None) -> list[dict]:
    sql = """
        SELECT date_trunc('week', created_at)::date AS week,
               AVG(score) AS avg_score,
               COUNT(*)::int AS n
        FROM ticket_audits
        WHERE org_id = %s
          AND created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, days]
    if only_user_id:
        sql += " AND agent_user_id = %s"
        params.append(only_user_id)
    sql += " GROUP BY 1 ORDER BY 1"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"week": _iso_week(r["week"]), "avg_score": _num(r.get("avg_score")), "count": _int(r.get("n"))}
        for r in rows
    ]


def _call_weeks(conn, org_id: str, days: int, *, only_user_id: str | None) -> list[dict]:
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (call_id) call_id, org_id, score, created_at
            FROM audits
            WHERE org_id = %s AND score IS NOT NULL
            ORDER BY call_id, created_at DESC
        )
        SELECT date_trunc('week', latest.created_at)::date AS week,
               AVG(latest.score) AS avg_score,
               COUNT(*)::int AS n
        FROM calls c
        INNER JOIN latest ON latest.call_id = c.id AND latest.org_id = c.org_id
        WHERE c.org_id = %s
          AND c.deleted_at IS NULL
          AND latest.created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, org_id, days]
    if only_user_id:
        sql += " AND c.agent_user_id = %s"
        params.append(only_user_id)
    sql += " GROUP BY 1 ORDER BY 1"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"week": _iso_week(r["week"]), "avg_score": _num(r.get("avg_score")), "count": _int(r.get("n"))}
        for r in rows
    ]


def _ticket_highlights(
    conn, org_id: str, days: int, *, only_user_id: str | None,
) -> dict[str, dict]:
    sql = """
        SELECT agent_user_id, findings
        FROM ticket_audits
        WHERE org_id = %s
          AND created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, days]
    if only_user_id:
        sql += " AND agent_user_id = %s"
        params.append(only_user_id)
    rows = conn.execute(sql, params).fetchall()
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        uid = parse_org_id(r.get("agent_user_id"))
        if not uid:
            continue
        by_agent.setdefault(uid, []).append(dict(r))
    out: dict[str, dict] = {}
    for uid, items in by_agent.items():
        out[uid] = {
            "top_strength": _mode_highlight(items, ticket_audit_summary.top_strength),
            "top_gap": _mode_highlight(items, ticket_audit_summary.top_gap),
        }
    return out


def _call_highlights(
    conn, org_id: str, days: int, *, only_user_id: str | None,
) -> dict[str, dict]:
    sql = """
        WITH latest AS (
            SELECT DISTINCT ON (call_id) call_id, org_id, findings, created_at
            FROM audits
            WHERE org_id = %s AND score IS NOT NULL
            ORDER BY call_id, created_at DESC
        )
        SELECT c.agent_user_id, latest.findings
        FROM calls c
        INNER JOIN latest ON latest.call_id = c.id AND latest.org_id = c.org_id
        WHERE c.org_id = %s
          AND c.deleted_at IS NULL
          AND latest.created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, org_id, days]
    if only_user_id:
        sql += " AND c.agent_user_id = %s"
        params.append(only_user_id)
    rows = conn.execute(sql, params).fetchall()
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        uid = parse_org_id(r.get("agent_user_id"))
        if not uid:
            continue
        by_agent.setdefault(uid, []).append(dict(r))
    out: dict[str, dict] = {}
    for uid, items in by_agent.items():
        out[uid] = {
            "top_strength": _mode_highlight(items, ticket_audit_summary.top_strength),
            "top_gap": _mode_highlight(items, ticket_audit_summary.top_gap),
        }
    return out


def _count(conn, sql: str, params: list) -> int:
    row = conn.execute(sql, params).fetchone()
    if not row:
        return 0
    return _int(row.get("n"))


def _ticket_total(conn, org_id: str, days: int, *, only_user_id: str | None) -> int:
    if only_user_id:
        return _count(
            conn,
            """
            SELECT COUNT(DISTINCT t.id)::int AS n
            FROM tickets t
            INNER JOIN ticket_messages m
              ON m.ticket_id = t.id AND m.org_id = t.org_id
            WHERE t.org_id = %s
              AND t.created_at >= now() - (%s * INTERVAL '1 day')
              AND m.agent_user_id = %s
            """,
            [org_id, days, only_user_id],
        )
    return _count(
        conn,
        """
        SELECT COUNT(*)::int AS n
        FROM tickets
        WHERE org_id = %s
          AND created_at >= now() - (%s * INTERVAL '1 day')
        """,
        [org_id, days],
    )


def _call_total(conn, org_id: str, days: int, *, only_user_id: str | None) -> int:
    sql = """
        SELECT COUNT(*)::int AS n
        FROM calls
        WHERE org_id = %s
          AND deleted_at IS NULL
          AND created_at >= now() - (%s * INTERVAL '1 day')
    """
    params: list = [org_id, days]
    if only_user_id:
        sql += " AND agent_user_id = %s"
        params.append(only_user_id)
    return _count(conn, sql, params)


def _org_totals(parts: list[dict]) -> dict:
    n = 0
    weighted = 0.0
    for p in parts:
        avg = p.get("avg_score")
        c = _int(p.get("count"))
        if avg is None or c <= 0:
            continue
        n += c
        weighted += float(avg) * c
    if n <= 0:
        return {"avg_score": None, "count": 0}
    return {"avg_score": round(weighted / n, 1), "count": n}


def _merge_weeks(ticket_weeks: list[dict], call_weeks: list[dict]) -> list[dict]:
    by_week: dict[str, dict] = {}
    for row in ticket_weeks:
        week = row["week"]
        by_week[week] = {
            "week": week,
            "ticket_avg": row.get("avg_score"),
            "ticket_count": row.get("count") or 0,
            "call_avg": None,
            "call_count": 0,
        }
    for row in call_weeks:
        week = row["week"]
        slot = by_week.setdefault(
            week,
            {"week": week, "ticket_avg": None, "ticket_count": 0, "call_avg": None, "call_count": 0},
        )
        slot["call_avg"] = row.get("avg_score")
        slot["call_count"] = row.get("count") or 0
    return [by_week[k] for k in sorted(by_week)]


def snapshot(
    org_id: str,
    *,
    viewer_user_id: str,
    is_manager: bool,
    days: int,
) -> dict:
    """One org-scoped dashboard payload. Members are scoped to themselves."""
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    if not oid or not vid:
        raise ValueError("org_id and viewer_user_id are required")
    n = clamp_days(days)
    only = None if is_manager else vid
    with org_scope(oid):
        with db.connection() as conn:
            members = _members(conn, oid, only_user_id=only)
            tickets = _ticket_stats(conn, oid, n, only_user_id=only)
            calls = _call_stats(conn, oid, n, only_user_id=only)
            t_weeks = _ticket_weeks(conn, oid, n, only_user_id=only)
            c_weeks = _call_weeks(conn, oid, n, only_user_id=only)
            highlights = _ticket_highlights(conn, oid, n, only_user_id=only)
            call_highlights = _call_highlights(conn, oid, n, only_user_id=only)
            ticket_total = _ticket_total(conn, oid, n, only_user_id=only)
            call_total = _call_total(conn, oid, n, only_user_id=only)

    agents = []
    seen: set[str] = set()
    for m in members:
        uid = m["user_id"]
        seen.add(uid)
        t = tickets.get(uid) or {"avg_score": None, "count": 0}
        c = calls.get(uid) or {"avg_score": None, "count": 0}
        h = highlights.get(uid) or {}
        ch = call_highlights.get(uid) or {}
        agents.append({
            "user_id": uid,
            "display_name": m["display_name"],
            "role": m["role"],
            "tickets": t,
            "calls": c,
            "top_strength": h.get("top_strength"),
            "top_gap": h.get("top_gap"),
            "call_top_strength": ch.get("top_strength"),
            "call_top_gap": ch.get("top_gap"),
        })
    if is_manager and _UNASSIGNED in calls:
        c = calls[_UNASSIGNED]
        agents.append({
            "user_id": None,
            "display_name": "Unassigned calls",
            "role": None,
            "tickets": {"avg_score": None, "count": 0},
            "calls": c,
            "top_strength": None,
            "top_gap": None,
            "call_top_strength": None,
            "call_top_gap": None,
        })
    # A scored agent who left the org still has audits — keep the row,
    # without inventing a roster identity beyond the UUID.
    leftover = (set(tickets) | set(calls) | set(highlights) | set(call_highlights)) - seen - {_UNASSIGNED}
    for uid in leftover:
        t = tickets.get(uid) or {"avg_score": None, "count": 0}
        c = calls.get(uid) or {"avg_score": None, "count": 0}
        h = highlights.get(uid) or {}
        ch = call_highlights.get(uid) or {}
        agents.append({
            "user_id": uid,
            "display_name": "Former teammate",
            "role": None,
            "tickets": t,
            "calls": c,
            "top_strength": h.get("top_strength"),
            "top_gap": h.get("top_gap"),
            "call_top_strength": ch.get("top_strength"),
            "call_top_gap": ch.get("top_gap"),
        })

    ticket_parts = [a["tickets"] for a in agents]
    call_parts = [a["calls"] for a in agents]
    t_org = _org_totals(ticket_parts)
    c_org = _org_totals(call_parts)
    t_org["total"] = ticket_total
    c_org["total"] = call_total
    return {
        "view_scope": "team" if is_manager else "own",
        "days": n,
        "org": {
            "tickets": t_org,
            "calls": c_org,
        },
        "weekly": _merge_weeks(t_weeks, c_weeks),
        "agents": agents,
    }
