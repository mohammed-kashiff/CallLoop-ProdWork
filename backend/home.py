"""GET /api/home — the logged-in person's week. Always self-scoped.

Owner/manager still see their own scores, flags, drills, and tickets —
not a team rollup. JWT org_id only. No RLS bypass.
"""

from __future__ import annotations

from . import audit_store
from . import db
from . import performance_kpis
from . import team_performance
from . import training
from .org_ids import org_scope, parse_org_id


def _open_flag(audit: object) -> bool:
    if not isinstance(audit, dict):
        return False
    if audit.get("review_solved"):
        return False
    return bool(
        audit.get("flagged")
        or audit.get("manual_review")
        or (audit.get("manager_review") or [])
        or (audit.get("gate_fails") or [])
    )


def _as_drill(row: dict) -> dict:
    prompt = str(row.get("prompt") or row.get("coaching_note") or row.get("reasoning") or "").strip()
    return {
        "kind": "assignment" if row.get("id") and row.get("status") else "suggested",
        "id": row.get("id"),
        "channel": row.get("channel"),
        "call_id": row.get("call_id"),
        "ticket_id": row.get("ticket_id"),
        "dimension_id": row.get("dimension_id"),
        "dimension_name": row.get("dimension_name"),
        "prompt": prompt,
        "status": row.get("status") or "open",
    }


def _self_scores(perf: dict, viewer_id: str, kpi_rows: list[dict]) -> dict:
    me = next(
        (a for a in (perf.get("agents") or []) if a.get("user_id") == viewer_id),
        None,
    )
    tickets = (me or {}).get("tickets") or {}
    calls = (me or {}).get("calls") or {}
    return {
        "tickets": {
            "avg_score": tickets.get("avg_score"),
            "count": tickets.get("count") or 0,
            "target": performance_kpis.effective_target(
                kpi_rows, "ticket", performance_kpis.OVERALL_ID, viewer_id,
            ),
        },
        "calls": {
            "avg_score": calls.get("avg_score"),
            "count": calls.get("count") or 0,
            "target": performance_kpis.effective_target(
                kpi_rows, "call", performance_kpis.OVERALL_ID, viewer_id,
            ),
        },
    }


def _open_flags(conn, org_id: str, viewer_id: str) -> list[dict]:
    join_sql = audit_store.latest_default_join_sql(inner=True)
    join_params = audit_store.latest_default_join_params(org_id)
    rows = conn.execute(
        f"""
        SELECT c.id, c.filename, a.findings
        FROM calls c
        {join_sql}
        WHERE c.org_id = %s AND c.deleted_at IS NULL AND c.agent_user_id = %s
        ORDER BY c.id DESC
        LIMIT 20
        """,
        (*join_params, org_id, viewer_id),
    ).fetchall()
    out = []
    for r in rows or []:
        audit = audit_store.decode_findings(r.get("findings"))
        if not _open_flag(audit):
            continue
        out.append({
            "call_id": int(r["id"]),
            "filename": (r.get("filename") or "").strip() or f"call-{r['id']}",
            "score": audit.get("score") if isinstance(audit, dict) else None,
        })
        if len(out) >= 5:
            break
    return out


def _recent_tickets(conn, org_id: str, viewer_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT t.id, t.status, t.created_at, t.subject
        FROM tickets t
        WHERE t.org_id = %s
          AND EXISTS (
            SELECT 1 FROM ticket_messages m
            WHERE m.ticket_id = t.id AND m.org_id = t.org_id
              AND m.agent_user_id = %s
          )
        ORDER BY t.created_at DESC
        LIMIT 3
        """,
        (org_id, viewer_id),
    ).fetchall()
    out = []
    for r in rows or []:
        tid = parse_org_id(r.get("id"))
        if not tid:
            continue
        created = r.get("created_at")
        out.append({
            "ticket_id": tid,
            "status": r.get("status"),
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "subject": r.get("subject"),
        })
    return out


def snapshot(org_id: str, viewer_user_id: str) -> dict:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    if not oid or not vid:
        raise ValueError("org_id and viewer_user_id are required")
    # Force member scope even for owners — Home is "your week".
    perf = team_performance.snapshot(
        oid, viewer_user_id=vid, is_manager=False, days=30,
    )
    train = training.snapshot(
        oid, viewer_user_id=vid, is_manager=False, days=30,
    )
    with org_scope(oid):
        with db.connection() as conn:
            flags = _open_flags(conn, oid, vid)
            open_assigned = training.list_open_assignments(conn, oid, vid, limit=3)
            tickets = _recent_tickets(conn, oid, vid)
            kpi_rows = performance_kpis.fetch_stored(conn, oid, only_agent_id=vid)
    drills = [_as_drill(r) for r in (open_assigned or (train.get("drills") or [])[:3])]
    return {
        "user_id": vid,
        "days": 30,
        "scores": _self_scores(perf, vid, kpi_rows),
        "flags": flags,
        "drills": drills,
        "tickets": tickets,
    }
