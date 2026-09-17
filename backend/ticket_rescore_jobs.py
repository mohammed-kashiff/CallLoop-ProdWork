"""Internal, platform-admin-triggered bulk re-score.

Given an org and a date range, re-scores every resolved agent on every
ready ticket in that range against the org's current rubric,
overwriting whatever is already stored.

Built as IN-27's own follow-on: fixing image-description tagging needed
a way to actually re-run scoring on already-audited tickets, which the
everyday product deliberately keeps gated behind enable_ticket_rescoring
(TA-33, permanently off by default — a ticket's score stays fixed once
set, on purpose). This tool is not that feature and does not touch that
flag at all, in either direction. It is a separate, internal-only,
explicitly-triggered action for the CallLoop team to correct a known
scoring bug's already-caused damage going forward — not something an
org's own owner/manager can reach, and not a way around their org's own
rescoring setting.

No queue/worker infrastructure exists in this codebase (single Render
instance, free plan). Like the JustCall/Intercom pollers, this runs as
a background thread in the same process, with progress tracked
in-memory — a job's state is lost on process restart, and only one job
is allowed to run at a time (ticket scoring is itself fully serial, one
real Claude call per rubric dimension per agent; running two jobs
concurrently would only make both slower and compete for the same rate
limits, never actually parallelize).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import date, datetime, timezone

from . import applog
from . import ticket_audit_store
from . import ticket_ingest
from . import ticket_rubric
from . import ticket_scoring
from .org_ids import parse_org_id

log = logging.getLogger("callproof.ticket_rescore_jobs")

_DEFAULT_SECONDS_PER_ITEM = 20.0  # rough starting guess, corrected from real timing as a job runs

_lock = threading.Lock()
_jobs: dict[str, dict] = {}


def _parse_date(value: str, *, field: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD).") from None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _any_job_running() -> bool:
    return any(j["status"] == "running" for j in _jobs.values())


def start_backfill(
    org_id: str, from_date: str, to_date: str, *, requested_by: str | None,
) -> dict:
    """Plan and launch one backfill job. The item count and initial ETA
    are computed from real data before any Claude call — resolved_agent_
    ids() only needs turns already in the DB — not a guess."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("A valid org_id is required.")
    f = _parse_date(from_date, field="from_date")
    t = _parse_date(to_date, field="to_date")
    if f > t:
        raise ValueError("from_date must not be after to_date.")

    with _lock:
        if _any_job_running():
            raise RuntimeError(
                "A ticket rescore backfill is already running. Wait for it to finish "
                "before starting another — ticket scoring is fully serial, so a "
                "second job would only slow both down, never run in parallel."
            )

    ticket_ids = ticket_ingest.list_ready_ticket_ids_in_range(oid, f, t)
    items: list[tuple[str, list[dict], list[str]]] = []  # (ticket_id, turns, agent_ids)
    for tid in ticket_ids:
        ticket = ticket_ingest.get_ticket(tid, oid)
        if not ticket or not ticket["messages"]:
            continue
        turns = [
            {
                "seq": m["seq"], "speaker": m["speaker"], "text": m["text"],
                "agent_user_id": m["agent_user_id"], "sent_at": m.get("sent_at"),
                "is_image_description": m.get("is_image_description", False),
            }
            for m in ticket["messages"]
        ]
        agent_ids = ticket_scoring.resolved_agent_ids(turns)
        if agent_ids:
            items.append((tid, turns, agent_ids))

    total_items = sum(len(agent_ids) for _tid, _turns, agent_ids in items)
    job_id = str(uuid.uuid4())
    job = {
        "job_id": job_id,
        "org_id": oid,
        "from_date": f.isoformat(),
        "to_date": t.isoformat(),
        "requested_by": requested_by,
        "status": "running" if total_items else "done",
        "total_tickets": len(items),
        "total_items": total_items,
        "completed_items": 0,
        "total_elapsed_seconds": 0.0,
        "seconds_per_item": _DEFAULT_SECONDS_PER_ITEM,
        "errors": [],
        "started_at": _iso_now(),
        "finished_at": None if total_items else _iso_now(),
    }
    with _lock:
        _jobs[job_id] = job

    if total_items:
        thread = threading.Thread(
            target=_run_backfill, args=(job_id, oid, items, requested_by),
            name=f"ticket-rescore-{job_id[:8]}", daemon=True,
        )
        thread.start()

    applog.event(
        log, "ticket_rescore_backfill_started",
        org_id=oid, job_id=job_id, total_tickets=len(items), total_items=total_items,
    )
    return get_job(job_id)


def _run_backfill(
    job_id: str, org_id: str,
    items: list[tuple[str, list[dict], list[str]]],
    requested_by: str | None,
) -> None:
    """completed_items counts every attempted ticket's agent-items,
    success or failure — it's the progress/ETA denominator (an item that
    failed is still no longer pending, and still took real time). errors
    is the separate signal for what actually went wrong."""
    for ticket_id, turns, agent_ids in items:
        item_started = time.monotonic()
        try:
            rubric = ticket_rubric.ensure_ticket_rubric(org_id)
            results = ticket_scoring.score_ticket_per_agent(
                turns, rubric["dimensions"], only_agent_ids=agent_ids,
            )
            ticket_audit_store.upsert_many(
                ticket_id, org_id, results, requested_by=requested_by,
            )
        except Exception as e:  # noqa: BLE001
            with _lock:
                _jobs[job_id]["errors"].append({
                    "ticket_id": ticket_id, "error": applog.safe_exception_text(e),
                })
            applog.event(
                log, "ticket_rescore_backfill_item_failed", level=logging.ERROR,
                job_id=job_id, ticket_id=ticket_id, error=applog.safe_exception_text(e),
            )
            continue
        finally:
            elapsed = time.monotonic() - item_started
            with _lock:
                job = _jobs[job_id]
                job["completed_items"] += len(agent_ids)
                job["total_elapsed_seconds"] += elapsed
                job["seconds_per_item"] = (
                    job["total_elapsed_seconds"] / job["completed_items"]
                )

    with _lock:
        job = _jobs[job_id]
        job["status"] = "done" if not job["errors"] else "done_with_errors"
        job["finished_at"] = _iso_now()
        completed, errors = job["completed_items"], len(job["errors"])
    applog.event(
        log, "ticket_rescore_backfill_finished",
        job_id=job_id, org_id=org_id, completed=completed, errors=errors,
    )


def get_job(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        remaining = max(job["total_items"] - job["completed_items"], 0)
        eta_seconds = round(remaining * job["seconds_per_item"]) if job["status"] == "running" else 0
        return {
            "job_id": job["job_id"],
            "org_id": job["org_id"],
            "from_date": job["from_date"],
            "to_date": job["to_date"],
            "status": job["status"],
            "total_tickets": job["total_tickets"],
            "total_items": job["total_items"],
            "completed_items": job["completed_items"],
            "estimated_seconds_remaining": eta_seconds,
            "started_at": job["started_at"],
            "finished_at": job["finished_at"],
            "errors": list(job["errors"]),
        }


def reset_for_tests() -> None:
    with _lock:
        _jobs.clear()
