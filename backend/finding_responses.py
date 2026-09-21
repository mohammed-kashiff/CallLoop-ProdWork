"""finding_responses: agent agree/dispute on a scored criterion.

One stance per (org, user, call|ticket, dimension). The scored agent
writes; owner/manager read disputes on Flagged for review. JWT org_id
only. Parameterized SQL. No RLS bypass.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from . import db
from .org_ids import org_scope, parse_org_id

_NOTE_MAX = 400
_DIM_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_CHANNELS = frozenset({"call", "ticket"})
_STANCES = frozenset({"agree", "dispute"})


def parse_channel(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in _CHANNELS:
        raise ValueError("channel must be call or ticket")
    return value


def parse_dim(raw: object) -> str:
    value = str(raw or "").strip()
    if not _DIM_RE.fullmatch(value):
        raise ValueError("dimension_id is not a valid dimension id")
    return value


def parse_stance(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value not in _STANCES:
        raise ValueError("stance must be agree or dispute")
    return value


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    return text or None


def _clip_note(raw: object) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return text[:_NOTE_MAX]


def _row(row: dict) -> dict:
    call_id = row.get("call_id")
    ticket_id = row.get("ticket_id")
    return {
        "id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "channel": row["channel"],
        "call_id": int(call_id) if call_id is not None else None,
        "ticket_id": str(ticket_id) if ticket_id else None,
        "dimension_id": str(row["dimension_id"]),
        "stance": row["stance"],
        "note": row.get("note"),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
        "display_name": row.get("display_name"),
        "filename": row.get("filename"),
        "subject": row.get("subject"),
    }


def _require_scored_agent(
    conn, org_id: str, viewer_id: str, channel: str, call_id: int | None, ticket_id: str | None,
) -> None:
    if channel == "call":
        row = conn.execute(
            """
            SELECT agent_user_id FROM calls
            WHERE id = %s AND org_id = %s AND deleted_at IS NULL
            """,
            (call_id, org_id),
        ).fetchone()
        if not row:
            raise LookupError("Call not found.")
        if parse_org_id(row.get("agent_user_id")) != viewer_id:
            raise PermissionError("Only the scored agent can respond to this finding.")
        return
    row = conn.execute(
        """
        SELECT agent_user_id FROM ticket_audits
        WHERE ticket_id = %s AND org_id = %s AND agent_user_id = %s
        """,
        (ticket_id, org_id, viewer_id),
    ).fetchone()
    if row:
        return
    exists = conn.execute(
        "SELECT 1 FROM tickets WHERE id = %s AND org_id = %s",
        (ticket_id, org_id),
    ).fetchone()
    if not exists:
        raise LookupError("Ticket not found.")
    raise PermissionError("Only the scored agent can respond to this finding.")


def upsert(
    org_id: str,
    *,
    viewer_user_id: str,
    channel: str,
    dimension_id: str,
    stance: str,
    note: object = None,
    call_id: int | None = None,
    ticket_id: str | None = None,
) -> dict:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    ch = parse_channel(channel)
    dim = parse_dim(dimension_id)
    st = parse_stance(stance)
    if not oid or not vid:
        raise ValueError("org_id and viewer_user_id are required")
    cid: int | None = None
    if call_id is not None and call_id != "":
        try:
            cid = int(call_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("call_id is not a valid call.") from exc
        if cid < 1:
            raise ValueError("call_id is not a valid call.")
    tid = parse_org_id(ticket_id) if ticket_id else None
    if ch == "call":
        if cid is None or tid is not None:
            raise ValueError("A call finding needs call_id only.")
    else:
        if tid is None or cid is not None:
            raise ValueError("A ticket finding needs ticket_id only.")
    clipped = _clip_note(note)
    if st == "dispute" and not clipped:
        raise ValueError("A short note is required when you dispute a finding.")
    if st == "agree":
        clipped = clipped  # optional on agree

    with org_scope(oid):
        with db.connection() as conn:
            _require_scored_agent(conn, oid, vid, ch, cid, tid)
            existing = conn.execute(
                """
                SELECT id FROM finding_responses
                WHERE org_id = %s AND user_id = %s AND dimension_id = %s
                  AND channel = %s
                  AND (
                    (channel = 'call' AND call_id = %s)
                    OR (channel = 'ticket' AND ticket_id = %s)
                  )
                """,
                (oid, vid, dim, ch, cid, tid),
            ).fetchone()
            if existing:
                row = conn.execute(
                    """
                    UPDATE finding_responses
                    SET stance = %s, note = %s, updated_at = now()
                    WHERE id = %s AND org_id = %s
                    RETURNING id, user_id, channel, call_id, ticket_id,
                              dimension_id, stance, note, created_at, updated_at
                    """,
                    (st, clipped, existing["id"], oid),
                ).fetchone()
            else:
                try:
                    row = conn.execute(
                        """
                        INSERT INTO finding_responses (
                            org_id, user_id, channel, call_id, ticket_id,
                            dimension_id, stance, note
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id, user_id, channel, call_id, ticket_id,
                                  dimension_id, stance, note, created_at, updated_at
                        """,
                        (oid, vid, ch, cid, tid, dim, st, clipped),
                    ).fetchone()
                except db.IntegrityError as exc:
                    raise LookupError("Could not save this response.") from exc
    if not row:
        raise LookupError("Could not save this response.")
    return _row(row)


def list_for_source(
    org_id: str,
    *,
    viewer_user_id: str,
    is_manager: bool,
    channel: str,
    call_id: int | None = None,
    ticket_id: str | None = None,
) -> list[dict]:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    ch = parse_channel(channel)
    if not oid or not vid:
        raise ValueError("org_id and viewer_user_id are required")
    cid: int | None = None
    if call_id is not None and call_id != "":
        cid = int(call_id)
    tid = parse_org_id(ticket_id) if ticket_id else None
    if ch == "call" and (cid is None or cid < 1):
        raise ValueError("call_id is required.")
    if ch == "ticket" and not tid:
        raise ValueError("ticket_id is required.")
    sql = """
        SELECT id, user_id, channel, call_id, ticket_id,
               dimension_id, stance, note, created_at, updated_at
        FROM finding_responses
        WHERE org_id = %s AND channel = %s
    """
    params: list = [oid, ch]
    if ch == "call":
        sql += " AND call_id = %s"
        params.append(cid)
    else:
        sql += " AND ticket_id = %s"
        params.append(tid)
    if not is_manager:
        sql += " AND user_id = %s"
        params.append(vid)
    sql += " ORDER BY updated_at DESC"
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
    return [_row(r) for r in rows or []]


def viewer_can_respond(
    org_id: str,
    *,
    viewer_user_id: str,
    channel: str,
    call_id: int | None = None,
    ticket_id: str | None = None,
) -> bool:
    oid = parse_org_id(org_id)
    vid = parse_org_id(viewer_user_id)
    try:
        ch = parse_channel(channel)
        cid = int(call_id) if call_id is not None and call_id != "" else None
        tid = parse_org_id(ticket_id) if ticket_id else None
        if not oid or not vid:
            return False
        with org_scope(oid):
            with db.connection() as conn:
                _require_scored_agent(conn, oid, vid, ch, cid, tid)
        return True
    except (PermissionError, LookupError, ValueError, TypeError):
        return False


def list_disputes(org_id: str) -> list[dict]:
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("org_id is required")
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT fr.id, fr.user_id, fr.channel, fr.call_id, fr.ticket_id,
                       fr.dimension_id, fr.stance, fr.note, fr.created_at, fr.updated_at,
                       NULLIF(TRIM(CONCAT_WS(' ', om.first_name, om.last_name)), '') AS display_name,
                       c.filename,
                       t.subject
                FROM finding_responses fr
                LEFT JOIN org_members om
                  ON om.org_id = fr.org_id AND om.user_id = fr.user_id
                LEFT JOIN calls c
                  ON c.id = fr.call_id AND c.org_id = fr.org_id
                LEFT JOIN tickets t
                  ON t.id = fr.ticket_id AND t.org_id = fr.org_id
                WHERE fr.org_id = %s AND fr.stance = 'dispute'
                ORDER BY fr.updated_at DESC
                LIMIT 50
                """,
                (oid,),
            ).fetchall()
    return [_row(r) for r in rows or []]
