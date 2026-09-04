"""Per-call pipeline audit trail (AC-24): every stage of upload -> transcribe
-> score -> serve, including every failure with its cause, as queryable
rows rather than just log lines.

record() is best-effort by design, same principle as password_events and
applog: a trail-write failure must never break the actual pipeline step it
was describing. It catches its own exceptions and falls back to a plain
applog line so the failure itself isn't silently lost.
"""

from __future__ import annotations

import json
import logging

from . import applog
from . import db
from .org_ids import org_scope, parse_org_id

log = logging.getLogger("callproof.call_trail")


def record(
    call_id: int,
    org_id: str | None,
    stage: str,
    status: str,
    *,
    detail: dict | None = None,
    error: str | None = None,
) -> None:
    """One row: call_id/org_id/stage/status, plus optional JSONB detail and
    a plain-text error (only meaningful when status='failed').

    status must be 'started' | 'succeeded' | 'failed' (matches the table's
    CHECK constraint) — an unexpected value is coerced to 'failed' with the
    original value folded into detail, rather than raising and losing the
    event entirely.
    """
    oid = parse_org_id(org_id)
    if not oid:
        applog.event(
            log, "call_trail_skipped", level=logging.WARNING,
            call_id=call_id, stage=stage, reason="no_org_id",
        )
        return
    if status not in ("started", "succeeded", "failed"):
        detail = {**(detail or {}), "_invalid_status": status}
        status = "failed"
    try:
        with org_scope(oid):
            with db.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO call_pipeline_events (
                        org_id, call_id, stage, status, detail, error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        oid, call_id, stage, status,
                        json.dumps(detail) if detail is not None else None,
                        error,
                    ),
                )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "call_trail_write_failed", level=logging.WARNING,
            call_id=call_id, stage=stage, status=status,
            error=applog.safe_exception_text(e),
        )


def history(call_id: int, org_id: str) -> list[dict]:
    """Full trail for one call, chronological."""
    oid = parse_org_id(org_id)
    if not oid:
        return []
    with org_scope(oid):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT stage, status, detail, error, created_at
                FROM call_pipeline_events
                WHERE call_id = %s AND org_id = %s
                ORDER BY created_at ASC
                """,
                (call_id, oid),
            ).fetchall()
    out: list[dict] = []
    for row in rows or []:
        created_at = row.get("created_at")
        detail = row.get("detail")
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except (TypeError, ValueError):
                detail = None
        out.append(
            {
                "stage": row.get("stage"),
                "status": row.get("status"),
                "detail": detail,
                "error": row.get("error"),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return out
