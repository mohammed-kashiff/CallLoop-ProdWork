"""Ticket Audit Engine scoring HTTP surface.

Fills a real gap between TA-9 (upload only — its own docstring says
"scoring is not triggered here") and TA-10's need for an actual
scorecard to render: nothing else in this codebase wires TA-6
(ticket_scoring.py) to an HTTP route. Kept in its own file rather than
added to ticket_api.py since that file is under active concurrent
development; `register()` is called separately from api.py.

POST /api/tickets/{ticket_id}/score scores every resolved agent on the
ticket independently against the org's "Ticket QA" rubric — a real
rubrics-table row (TA-13, PRD §10), created on first use via
ticket_rubric.ensure_ticket_rubric() — via
ticket_scoring.score_ticket_per_agent() (TA-21/TA-25). Response
Timeliness (TA-13/TA-27) is computed deterministically from real message
timestamps, per agent, and appended to each agent's findings list
separately — it is not part of score_ticket_for_agent()'s weighted score
and is never persisted (recomputed fresh on every response).

TA-11 (PRD §9), rebuilt per-agent (TA-28): the rescoring guard is now
per agent, not per ticket. An agent who already has a stored row is
skipped unless ?refresh=true and the org's enable_ticket_rescoring flag
is on — same rule as enable_call_rescoring, so Claude's non-determinism
cannot quietly change a stored score. A newly-resolved agent with no row
yet still gets a real first score on a plain POST, even if other agents
on the same ticket were already audited — that's a genuine first score
for them, not a re-score of already-audited data.

POST, not GET: a first-time score costs real money per call, so it must
not be something a browser could trigger accidentally (a prefetch, a
refresh) the way a safe GET could.
"""

from __future__ import annotations

import logging
import uuid

from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import auth
from . import org_features
from . import rate_limit
from . import sentry_report
from . import ticket_audit_store
from . import ticket_audit_summary
from . import ticket_ingest
from . import ticket_permissions
from . import ticket_rubric
from . import ticket_rubric_builder
from . import ticket_scoring
from . import ticket_trail

log = logging.getLogger("callproof.ticket_score_api")

_RESCORE_DENIED = (
    "This ticket has already been audited. Re-scoring is disabled for this org."
)


def _parse_ticket_id(ticket_id: str) -> str:
    try:
        return str(uuid.UUID(str(ticket_id or "").strip()))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid ticket id.") from None


def _agent_with_timeliness(agent_result: dict, turns: list[dict]) -> dict:
    """TA-13/TA-27: Response Timeliness is deterministic, computed fresh
    from this ticket's real message timestamps every time, scoped to this
    one agent's own replies — never persisted (see ticket_rubric.
    evaluate_response_timeliness()'s own docstring), never part of the
    weighted score."""
    timeliness = ticket_rubric.evaluate_response_timeliness(
        turns, target_agent_user_id=agent_result["agent_user_id"],
    )
    return {**agent_result, "findings": [*(agent_result.get("findings") or []), timeliness]}


def _with_summary(agent_result: dict) -> dict:
    """IN-12: audit_summary/top_strength/top_gap computed from this one
    agent's own findings only — never persisted (ticket_audit_summary.py's
    own docstring), never mixed with another agent's scoring output."""
    return ticket_audit_summary.enrich(agent_result)


def _payload(
    tid: str, agent_results: list[dict], turns: list[dict],
    *, cached: bool, viewer_user_id: str, is_manager: bool,
) -> dict:
    """TA-29: a manager (owner or manager, AC-56/AC-60) gets every agent's
    independent scorecard. Anyone else gets only their own — never a
    teammate's individual score, even on a ticket they share. The full
    thread itself is never filtered here (TA-30) — that's the caller's
    job via ticket_ingest.get_ticket(), which always returns every turn
    regardless of viewer; this payload is scores only.
    """
    visible = ticket_permissions.filter_audits_for_viewer(
        agent_results, viewer_user_id=viewer_user_id, is_manager=is_manager,
    )
    agents = [_with_summary(_agent_with_timeliness(a, turns)) for a in visible]
    return {
        "ticket_id": tid,
        "cached": cached,
        "view_scope": "full" if is_manager else "own",
        "agents": agents,
    }


def ticket_rubric_route(request: Request):
    """Read-only: this org's active "Ticket QA" rubric, described the same
    way rubric_builder.current_rubric() describes the call rubric — for
    the account-menu Rubric viewer. Any authenticated org member can view;
    not editable from this route (seeds the default via
    ensure_ticket_rubric() the first time, same as scoring does)."""
    org_id = auth.org_id_from_request(request)
    rubric = ticket_rubric.ensure_ticket_rubric(org_id)
    return {
        "name": rubric.get("name") or ticket_rubric.TICKET_QA_RUBRIC_NAME,
        "version": rubric.get("version"),
        "dimensions": rubric["dimensions"],
    }


# ---------- TA-24 follow-on: self-serve ticket rubric builder ----------
# Same customer-facing, owner-gated shape as /api/rubric[s] (api.py,
# rubric_builder.py) — mix of built-in and free-text custom dimensions,
# named, saved, multiple lineages, choose-which-is-active — for kind=
# "ticket" rows via ticket_rubric_builder.py. Kept under /api/tickets/
# rubric[s] rather than reusing the call paths: same URL shape one level
# down, distinct from the read-only /api/tickets/rubric viewer above.


class TicketRubricDimensionBody(BaseModel):
    kind: str
    id: str | None = None
    name: str | None = None
    question: str | None = None
    weight: int
    customer_facing_only: bool = False


class SaveTicketRubricBody(BaseModel):
    dimensions: list[TicketRubricDimensionBody]


def ticket_rubric_builder_route(request: Request):
    """The org's active Ticket QA rubric, described for the builder editor
    (builtin/custom picks + available_builtins) — any authenticated org
    member can view, same as GET /api/rubric for calls."""
    return ticket_rubric_builder.current_rubric(auth.org_id_from_request(request))


def save_ticket_rubric_route(request: Request, body: SaveTicketRubricBody):
    """Save a new version of the org's own ticket rubric under whatever
    name is currently active (or "Ticket QA" for a first-ever save).
    Owner or manager (AC-56/AC-61), same gate as the call rubric builder."""
    auth.require_owner_or_manager(request)
    return ticket_rubric_builder.save_rubric(
        auth.org_id_from_request(request),
        [d.model_dump() for d in body.dimensions],
        changed_by=getattr(request.state, "email", None) or "",
    )


def list_ticket_rubrics_route(request: Request):
    """Every named ticket rubric this org has saved — the library view."""
    return ticket_rubric_builder.list_rubrics(auth.org_id_from_request(request))


def get_ticket_rubric_by_name_route(request: Request, name: str):
    """One named ticket rubric's latest version, for loading into the editor."""
    return ticket_rubric_builder.get_rubric(auth.org_id_from_request(request), name)


class SaveNamedTicketRubricBody(BaseModel):
    dimensions: list[TicketRubricDimensionBody]
    activate: bool = True


def save_named_ticket_rubric_route(request: Request, name: str, body: SaveNamedTicketRubricBody):
    """Save a new version under this specific ticket rubric name — a
    library entry, not necessarily replacing whatever's currently active.
    Owner or manager."""
    auth.require_owner_or_manager(request)
    return ticket_rubric_builder.save_rubric(
        auth.org_id_from_request(request),
        [d.model_dump() for d in body.dimensions],
        changed_by=getattr(request.state, "email", None) or "",
        name=name,
        activate=body.activate,
    )


def activate_ticket_rubric_route(request: Request, name: str):
    """Switch which saved ticket rubric scores tickets going forward — no
    dimension change, just a swap. Owner or manager."""
    auth.require_owner_or_manager(request)
    return ticket_rubric_builder.activate_rubric(
        auth.org_id_from_request(request), name,
        changed_by=getattr(request.state, "email", None) or "",
    )


def score_ticket_route(request: Request, ticket_id: str, refresh: bool = False):
    org_id = auth.org_id_from_request(request)
    tid = _parse_ticket_id(ticket_id)
    viewer_id = auth.user_id_from_request(request)
    is_manager = auth.is_owner_or_manager(request)  # TA-12/AC-60

    agent_results, turns, cached = _score_ticket_core(
        org_id, tid, refresh=refresh, triggered_by="manual",
        requested_by=getattr(request.state, "user_id", None),
    )
    return _payload(
        tid, agent_results, turns, cached=cached, viewer_user_id=viewer_id, is_manager=is_manager,
    )


def auto_audit_ticket(org_id: str, ticket_id: str) -> None:
    """IN-33: Intercom ingestion's own trigger for Auto Audit
    (enable_ticket_auto_audit, IN-30). Checks the flag itself so
    intercom_ingest.py stays unaware of scoring policy — call this
    unconditionally at the end of ingestion.

    Never raises. Any reason the shared core wouldn't score (not ready,
    no resolved agent identities yet, rescore blocked, rate-limited)
    surfaces there as an HTTPException; here it just means auto-audit
    quietly doesn't fire this time — the ticket stays ready/unscored and
    falls back to the manual path, which is the correct safety net, not
    a gap (PRD §4.3). A real, unexpected failure is logged and reported
    to Sentry, but still never propagates into the ingestion call site.
    """
    if not org_features.features_for_org(org_id).get("enable_ticket_auto_audit"):
        return
    try:
        _score_ticket_core(
            org_id, ticket_id, refresh=False, triggered_by="auto", requested_by=None,
        )
    except HTTPException as e:
        applog.event(
            log, "ticket_auto_audit_skipped",
            ticket_id=ticket_id, org_id=org_id, status=e.status_code, reason=e.detail,
        )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "ticket_auto_audit_failed", level=logging.ERROR,
            ticket_id=ticket_id, org_id=org_id, error=applog.safe_exception_text(e),
        )
        sentry_report.capture_exception(e)


def _score_ticket_core(
    org_id: str,
    tid: str,
    *,
    refresh: bool,
    triggered_by: str,
    requested_by: str | None,
) -> tuple[list[dict], list[dict], bool]:
    """The actual scoring logic, shared by score_ticket_route (manual,
    triggered_by="manual") and auto_audit_ticket (triggered_by="auto",
    IN-32/IN-33) — reused, not duplicated, per the PRD. Raises
    HTTPException for every reason scoring didn't happen; the HTTP route
    lets that propagate as the response, auto_audit_ticket() catches it.

    Returns (agent_results, turns, cached) — unfiltered by viewer
    permission; score_ticket_route applies that via _payload(). Callers
    with no viewer (auto-audit) just discard the return value.
    """
    # AC-72: a real Claude call per agent per ticket — generous but real.
    # Same bucket/key regardless of triggered_by, so auto-audit queues
    # behind manual scoring's existing budget rather than a separate one.
    rate_limit.enforce("ticket_score", org_id, limit=60, window_seconds=300)

    ticket = ticket_ingest.get_ticket(tid, org_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")

    def _score_detail(extra: dict | None = None) -> dict:
        return ticket_trail.with_apis(
            {"refresh": bool(refresh), **(extra or {})},
            ticket_trail.API_TICKET_SCORE,
        )

    ticket_trail.record(
        tid, org_id, "scoring", "started", detail=_score_detail(),
    )

    def _scoring_failed(status_code: int, message: str, extra: dict | None = None):
        ticket_trail.record(
            tid, org_id, "scoring", "failed",
            detail=_score_detail(extra),
            error=message,
        )
        raise HTTPException(status_code=status_code, detail=message)

    if ticket["status"] == "failed":
        _scoring_failed(400, "This ticket failed ingestion; there is nothing to score.")
    if ticket["status"] != "ready":
        _scoring_failed(409, "This ticket is still processing.")
    if not ticket["messages"]:
        _scoring_failed(400, "This ticket has no messages to score.")

    turns = [
        {
            "seq": m["seq"],
            "speaker": m["speaker"],
            "text": m["text"],
            "agent_user_id": m["agent_user_id"],
            "sent_at": m.get("sent_at"),
        }
        for m in ticket["messages"]
    ]

    resolved = ticket_scoring.resolved_agent_ids(turns)
    if not resolved:
        _scoring_failed(
            400,
            "No agent identities are resolved on this ticket yet — map agent names first.",
        )

    stored_by_agent = {
        row["agent_user_id"]: row for row in ticket_audit_store.fetch_all(tid, org_id)
    }
    if refresh and stored_by_agent:
        if not org_features.features_for_org(org_id).get("enable_ticket_rescoring"):
            applog.event(
                log, "ticket_rescore_blocked",
                ticket_id=tid, agents=list(stored_by_agent),
            )
            _scoring_failed(403, _RESCORE_DENIED, extra={"refresh": True})
        to_score = resolved  # allowed re-score: every resolved agent runs fresh
    else:
        # TA-28: the guard is per-agent — an agent with no stored row yet
        # always gets a real first score, even if a teammate on the same
        # ticket was already audited.
        to_score = [a for a in resolved if a not in stored_by_agent]

    def _on_dimension_event(dim, status, detail):
        did = (dim or {}).get("id") or "unknown"
        ticket_trail.record(
            tid, org_id, f"criterion:{did}", status,
            detail=ticket_trail.with_apis(detail, ticket_trail.API_ANTHROPIC_MESSAGES),
        )

    fresh_results: list[dict] = []
    if to_score:
        try:
            rubric = ticket_rubric.ensure_ticket_rubric(org_id)
            fresh_results = ticket_scoring.score_ticket_per_agent(
                turns, rubric["dimensions"], only_agent_ids=to_score,
                on_dimension_event=_on_dimension_event,
            )
        except Exception as e:  # noqa: BLE001
            applog.event(
                log, "ticket_scoring_failed", level=logging.ERROR,
                ticket_id=tid, error=applog.safe_exception_text(e),
            )
            sentry_report.capture_exception(e)
            ticket_trail.record(
                tid, org_id, "scoring", "failed",
                detail=_score_detail({"agents": to_score}),
                error=applog.safe_exception_text(e),
            )
            raise HTTPException(status_code=502, detail="Ticket scoring failed.") from None

        for r in fresh_results:
            r["triggered_by"] = triggered_by

        try:
            ticket_audit_store.upsert_many(
                tid, org_id, fresh_results,
                requested_by=requested_by, triggered_by=triggered_by,
            )
        except Exception as e:  # noqa: BLE001
            applog.event(
                log, "ticket_audit_persist_failed", level=logging.ERROR,
                ticket_id=tid, error=applog.safe_exception_text(e),
            )
            sentry_report.capture_exception(e)
            ticket_trail.record(
                tid, org_id, "scoring", "failed",
                detail=_score_detail(),
                error=applog.safe_exception_text(e),
            )
            raise HTTPException(status_code=502, detail="Ticket scoring failed.") from None

        applog.event(
            log, "ticket_scored",
            ticket_id=tid, agents=[r["agent_user_id"] for r in fresh_results],
            refresh=bool(refresh),
        )

    fresh_by_agent = {r["agent_user_id"]: r for r in fresh_results}
    agent_results = [
        fresh_by_agent[a] if a in fresh_by_agent else stored_by_agent[a]
        for a in resolved
    ]
    cached = not fresh_results
    if cached:
        applog.event(log, "ticket_audit_cache", result="HIT", ticket_id=tid, agents=resolved)

    skipped = [a for a in resolved if a not in (to_score or [])]
    ticket_trail.record(
        tid, org_id, "scoring", "succeeded",
        detail=_score_detail({
            "cached": cached,
            "scored": [r["agent_user_id"] for r in fresh_results],
            "skipped": skipped,
        }),
    )
    ticket_trail.record(
        tid, org_id, "result_served", "succeeded",
        detail=ticket_trail.with_apis(
            {"source": "cache" if cached else "fresh"},
            ticket_trail.API_TICKET_SCORE,
        ),
    )

    return agent_results, turns, cached


_BULK_AUDIT_MAX_PER_CALL = 50  # stays under the 60/300s ticket_score rate limit
_BULK_AUDIT_WORKERS = 5  # modest — each ticket can be several real Claude calls


def audit_all_tickets_route(request: Request):
    """Owner/manager only: score every ready, not-yet-audited ticket in one
    go, from the Audits page's "Audit all" button. Reuses score_ticket_route
    per ticket (same rate limit, same per-agent skip-if-already-scored
    logic) rather than duplicating its scoring/persistence logic — a ticket
    a teammate already fully scored costs nothing extra here.

    Capped at _BULK_AUDIT_MAX_PER_CALL per call to stay under AC-72's
    60-per-5-minutes rate limit; any remainder is reported back so the
    frontend can offer "click again" rather than silently dropping work."""
    auth.require_owner_or_manager(request)
    org_id = auth.org_id_from_request(request)
    tickets = ticket_ingest.list_tickets(org_id)
    candidates = [t for t in tickets if t.get("status") == "ready" and not t.get("has_audit")]
    batch = candidates[:_BULK_AUDIT_MAX_PER_CALL]
    remaining = max(0, len(candidates) - len(batch))

    def _score_one(ticket_id: str) -> dict:
        try:
            score_ticket_route(request, ticket_id, refresh=False)
            return {"ticket_id": ticket_id, "status": "scored"}
        except HTTPException as e:
            return {
                "ticket_id": ticket_id, "status": "error",
                "error": e.detail, "status_code": e.status_code,
            }

    results: list[dict] = []
    if batch:
        with ThreadPoolExecutor(max_workers=min(_BULK_AUDIT_WORKERS, len(batch))) as pool:
            futs = {pool.submit(_score_one, t["id"]): t["id"] for t in batch}
            for fut in as_completed(futs):
                results.append(fut.result())

    scored = sum(1 for r in results if r["status"] == "scored")
    errors = [r for r in results if r["status"] == "error"]
    applog.event(
        log, "ticket_audit_all",
        org_id=org_id, candidates=len(candidates), attempted=len(batch),
        scored=scored, errors=len(errors), remaining=remaining,
    )
    return {
        "candidates": len(candidates),
        "attempted": len(batch),
        "scored": scored,
        "errors": errors,
        "remaining": remaining,
    }


def register(app) -> None:
    app.add_api_route("/api/tickets/rubric", ticket_rubric_route, methods=["GET"])
    app.add_api_route(
        "/api/tickets/rubric/builder", ticket_rubric_builder_route, methods=["GET"],
    )
    app.add_api_route(
        "/api/tickets/rubric/builder", save_ticket_rubric_route, methods=["POST"],
    )
    app.add_api_route("/api/tickets/rubrics", list_ticket_rubrics_route, methods=["GET"])
    app.add_api_route(
        "/api/tickets/rubrics/{name}", get_ticket_rubric_by_name_route, methods=["GET"],
    )
    app.add_api_route(
        "/api/tickets/rubrics/{name}", save_named_ticket_rubric_route, methods=["POST"],
    )
    app.add_api_route(
        "/api/tickets/rubrics/{name}/activate", activate_ticket_rubric_route, methods=["POST"],
    )
    app.add_api_route(
        "/api/tickets/{ticket_id}/score", score_ticket_route, methods=["POST"],
    )
    app.add_api_route(
        "/api/tickets/audit-all", audit_all_tickets_route, methods=["POST"],
    )
