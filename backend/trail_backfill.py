"""Reconstruct collapsed pipeline trails from stored scorecards.

Usage:
    python -m backend.trail_backfill
    python -m backend.trail_backfill --dry-run
    python -m backend.trail_backfill --calls-only
    python -m backend.trail_backfill --tickets-only

Live `call_pipeline_events` / `ticket_pipeline_events` rows are written
during ingest → score → serve. Calls and tickets scored before that
instrumentation have a scorecard (`audits` / `ticket_audits`) but an
empty trail. This CLI fills those gaps only.

It does not invent a live pipeline:
- Skip any call/ticket that already has trail rows (including a lone
  `result_served` from a later view).
- One collapsed outcome: transcription/parse succeeded, each stored
  criterion, recap only when the scorecard has one, scoring succeeded.
- Same scorecard timestamp on every step (microsecond offsets so ORDER
  BY created_at is stable). The rail shows these as Reconstructed.
- No `result_served`, no HTTP `apis`, no evidence quotes, no retries.

bypass_rls is the same Alembic-adjacent hatch as audio_backfill: this
module is not an API handler.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import db
from .audit_store import decode_findings
from .config import load_env

log = logging.getLogger("callproof.trail_backfill")

SOURCE = "backfill"

_CALLS_SQL = """
        SELECT DISTINCT ON (a.org_id, a.call_id)
            a.org_id,
            a.call_id,
            a.score,
            a.findings,
            a.created_at AS scored_at
        FROM audits a
        JOIN calls c ON c.id = a.call_id AND c.org_id = a.org_id
        WHERE c.deleted_at IS NULL
          AND NOT EXISTS (
            SELECT 1 FROM call_pipeline_events e
            WHERE e.call_id = a.call_id AND e.org_id = a.org_id
          )
        ORDER BY a.org_id, a.call_id, a.created_at DESC
        """

_TICKETS_SQL = """
        SELECT
            ta.org_id,
            ta.ticket_id,
            ta.agent_user_id,
            ta.score,
            ta.findings,
            ta.created_at AS scored_at
        FROM ticket_audits ta
        JOIN tickets t ON t.id = ta.ticket_id AND t.org_id = ta.org_id
        WHERE NOT EXISTS (
            SELECT 1 FROM ticket_pipeline_events e
            WHERE e.ticket_id = ta.ticket_id AND e.org_id = ta.org_id
          )
        ORDER BY ta.org_id, ta.ticket_id, ta.created_at ASC
        """

_INSERT_CALL = """
        INSERT INTO call_pipeline_events (
            org_id, call_id, stage, status, detail, error, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """

_INSERT_TICKET = """
        INSERT INTO ticket_pipeline_events (
            org_id, ticket_id, stage, status, detail, error, created_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        """


def stamp(extra: dict | None = None) -> dict:
    out = dict(extra or {})
    out["source"] = SOURCE
    out["reconstructed"] = True
    return out


def _criterion_events(
    findings: list,
    *,
    agent_user_id: str | None = None,
) -> list[tuple[str, str, dict, str | None]]:
    events: list[tuple[str, str, dict, str | None]] = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        cid = item.get("id")
        if not isinstance(cid, str) or not cid.strip():
            continue
        cid = cid.strip()
        verdict = item.get("verdict")
        status = "failed" if verdict == "error" else "succeeded"
        detail: dict[str, Any] = {}
        if verdict is not None:
            detail["verdict"] = verdict
        if agent_user_id:
            detail["agent_user_id"] = agent_user_id
        error = "criterion_error" if status == "failed" else None
        events.append((f"criterion:{cid}", status, stamp(detail), error))
    return events


def call_events_from_scorecard(
    payload: dict | None,
    *,
    score: Any = None,
) -> list[tuple[str, str, dict, str | None]]:
    """Collapsed call trail from one audits.findings blob. No evidence text."""
    data = payload if isinstance(payload, dict) else {}
    events: list[tuple[str, str, dict, str | None]] = [
        ("transcription", "succeeded", stamp(), None),
    ]
    nested = data.get("findings")
    if isinstance(nested, list):
        events.extend(_criterion_events(nested))
    recap = data.get("recap")
    if isinstance(recap, dict):
        recap_status = recap.get("status")
        if recap_status == "ok":
            events.append(("recap", "succeeded", stamp({"status": "ok"}), None))
        elif recap_status in ("error", "failed"):
            events.append(
                ("recap", "failed", stamp({"status": recap_status}), "recap_unavailable"),
            )
    scoring_detail: dict[str, Any] = {}
    sc = data.get("score") if score is None else score
    if sc is not None:
        scoring_detail["score"] = sc
    if data.get("grade") is not None:
        scoring_detail["grade"] = data.get("grade")
    if "flagged" in data:
        scoring_detail["flagged"] = bool(data.get("flagged"))
    events.append(("scoring", "succeeded", stamp(scoring_detail), None))
    return events


def ticket_events_from_scorecards(
    agent_rows: list[dict],
) -> list[tuple[str, str, dict, str | None]]:
    """Collapsed ticket trail from ticket_audits rows for one ticket."""
    events: list[tuple[str, str, dict, str | None]] = [
        ("parse", "succeeded", stamp(), None),
    ]
    agent_ids: list[str] = []
    for row in agent_rows:
        raw = row.get("agent_user_id")
        if raw:
            agent_ids.append(str(raw))
    if agent_ids:
        events.append(
            ("agent_resolve", "succeeded", stamp({"resolved": len(set(agent_ids))}), None),
        )
    scored: list[str] = []
    for row in agent_rows:
        uid = str(row["agent_user_id"]) if row.get("agent_user_id") else None
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        nested = payload.get("findings")
        if isinstance(nested, list):
            events.extend(_criterion_events(nested, agent_user_id=uid))
        if uid:
            scored.append(uid)
    events.append(
        ("scoring", "succeeded", stamp({"scored": scored}), None),
    )
    return events


def _when(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.now(timezone.utc)


def _stagger(when: datetime, index: int) -> datetime:
    return when + timedelta(microseconds=index)


def _dump(detail: dict) -> str:
    return json.dumps(detail)


def _insert_call_rows(conn, org_id: str, call_id: int, events, when: datetime) -> int:
    n = 0
    for i, (stage, status, detail, error) in enumerate(events):
        conn.execute(
            _INSERT_CALL,
            (org_id, call_id, stage, status, _dump(detail), error, _stagger(when, i)),
        )
        n += 1
    return n


def _insert_ticket_rows(conn, org_id: str, ticket_id: str, events, when: datetime) -> int:
    n = 0
    for i, (stage, status, detail, error) in enumerate(events):
        conn.execute(
            _INSERT_TICKET,
            (org_id, ticket_id, stage, status, _dump(detail), error, _stagger(when, i)),
        )
        n += 1
    return n


def backfill_calls(conn, *, dry_run: bool, limit: int | None) -> dict[str, int]:
    stats = {"candidates": 0, "written": 0, "events": 0, "skipped_empty": 0}
    rows = list(conn.execute(_CALLS_SQL).fetchall() or [])
    if limit is not None:
        rows = rows[: max(0, limit)]
    stats["candidates"] = len(rows)
    for row in rows:
        payload = decode_findings(row.get("findings"))
        events = call_events_from_scorecard(payload, score=row.get("score"))
        if not events:
            stats["skipped_empty"] += 1
            continue
        org_id = str(row["org_id"])
        call_id = int(row["call_id"])
        when = _when(row.get("scored_at"))
        if dry_run:
            stats["written"] += 1
            stats["events"] += len(events)
            continue
        n = _insert_call_rows(conn, org_id, call_id, events, when)
        stats["written"] += 1
        stats["events"] += n
        log.info("call trail backfilled call_id=%s org_id=%s events=%s", call_id, org_id, n)
    return stats


def backfill_tickets(conn, *, dry_run: bool, limit: int | None) -> dict[str, int]:
    stats = {"candidates": 0, "written": 0, "events": 0, "skipped_empty": 0}
    rows = list(conn.execute(_TICKETS_SQL).fetchall() or [])
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    times: dict[tuple[str, str], datetime] = {}
    for row in rows:
        key = (str(row["org_id"]), str(row["ticket_id"]))
        payload = decode_findings(row.get("findings")) or {}
        grouped[key].append({
            "agent_user_id": row.get("agent_user_id"),
            "score": row.get("score"),
            "payload": payload,
        })
        when = _when(row.get("scored_at"))
        prev = times.get(key)
        if prev is None or when > prev:
            times[key] = when
    keys = list(grouped.keys())
    if limit is not None:
        keys = keys[: max(0, limit)]
    stats["candidates"] = len(keys)
    for key in keys:
        org_id, ticket_id = key
        events = ticket_events_from_scorecards(grouped[key])
        if not events:
            stats["skipped_empty"] += 1
            continue
        when = times[key]
        if dry_run:
            stats["written"] += 1
            stats["events"] += len(events)
            continue
        n = _insert_ticket_rows(conn, org_id, ticket_id, events, when)
        stats["written"] += 1
        stats["events"] += n
        log.info(
            "ticket trail backfilled ticket_id=%s org_id=%s events=%s",
            ticket_id, org_id, n,
        )
    return stats


def backfill(
    *,
    dry_run: bool = False,
    calls: bool = True,
    tickets: bool = True,
    limit: int | None = None,
) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    with db.connection(bypass_rls=True) as conn:
        if calls:
            out["calls"] = backfill_calls(conn, dry_run=dry_run, limit=limit)
        if tickets:
            out["tickets"] = backfill_tickets(conn, dry_run=dry_run, limit=limit)
    return out


def main(argv: list[str] | None = None) -> int:
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Reconstruct collapsed pipeline trails from stored scorecards.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count candidates; do not insert.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--calls-only", action="store_true")
    group.add_argument("--tickets-only", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max calls and/or tickets to write (each side).",
    )
    args = parser.parse_args(argv)
    try:
        db.require_database_url()
    except RuntimeError as e:
        log.error("%s", e)
        return 2
    stats = backfill(
        dry_run=args.dry_run,
        calls=not args.tickets_only,
        tickets=not args.calls_only,
        limit=args.limit,
    )
    log.info(
        "trail backfill done dry_run=%s stats=%s",
        args.dry_run, json.dumps(stats),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
