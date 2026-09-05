"""Ticket Audit Engine scoring HTTP surface.

Fills a real gap between TA-9 (upload only — its own docstring says
"scoring is not triggered here") and TA-10's need for an actual
scorecard to render: nothing else in this codebase wires TA-6
(ticket_scoring.py) to an HTTP route. Kept in its own file rather than
added to ticket_api.py since that file is under active concurrent
development; `register()` is called separately from api.py.

POST /api/tickets/{ticket_id}/score scores every turn against the org's
"Ticket QA" rubric — a real rubrics-table row (TA-13, PRD §10), created
on first use via ticket_rubric.ensure_ticket_rubric() — via
ticket_scoring.score_ticket(). *** Still not the final rubric design,
see ticket_rubric.py's own scaffolding note. *** Response Timeliness
(TA-13) is computed deterministically from real message timestamps and
appended to the findings list separately — it is not part of
score_ticket()'s weighted score for v1.

TA-11 (PRD §9): the first successful POST persists the scorecard in
ticket_audits. A later POST without ?refresh=true returns that stored
result and does not call Claude. ?refresh=true is blocked with 403
unless the org's enable_ticket_rescoring flag is on (off by default) —
same rule as enable_call_rescoring, so Claude's non-determinism cannot
quietly change a stored ticket score.

POST, not GET: a first-time score costs real money per call, so it must
not be something a browser could trigger accidentally (a prefetch, a
refresh) the way a safe GET could.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, Request

from . import applog
from . import auth
from . import org_features
from . import sentry_report
from . import ticket_audit_store
from . import ticket_ingest
from . import ticket_permissions
from . import ticket_rubric
from . import ticket_scoring

log = logging.getLogger("callproof.ticket_score_api")

_RESCORE_DENIED = (
    "This ticket has already been audited. Re-scoring is disabled for this org."
)


def _parse_ticket_id(ticket_id: str) -> str:
    try:
        return str(uuid.UUID(str(ticket_id or "").strip()))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid ticket id.") from None


def _with_timeliness(payload: dict, turns: list[dict]) -> dict:
    """TA-13: Response Timeliness is deterministic, computed fresh from the
    ticket's real message timestamps every time — never persisted, never
    part of score_ticket()'s weighted score (v1; see ticket_rubric.
    evaluate_response_timeliness()'s own docstring), and never filtered by
    TA-12's own-contribution view: it's a whole-thread metric, not one
    agent's individual score, so it's appended after _payload() has
    already applied that filtering to everything else."""
    timeliness = ticket_rubric.evaluate_response_timeliness(turns)
    return {**payload, "findings": [*(payload.get("findings") or []), timeliness]}


def _payload(
    tid: str, result: dict, *, cached: bool, viewer_user_id: str, is_manager: bool,
) -> dict:
    """TA-12: a manager (org owner) gets every finding/span. Anyone else
    gets only the ones attributed to their own agent_user_id — never
    another agent's individual scores, even on a ticket they share."""
    filtered = {
        **result,
        "findings": ticket_permissions.filter_findings_for_viewer(
            result.get("findings") or [], viewer_user_id=viewer_user_id, is_manager=is_manager,
        ),
        "spans": ticket_permissions.filter_spans_for_viewer(
            result.get("spans") or [], viewer_user_id=viewer_user_id, is_manager=is_manager,
        ),
    }
    return {
        "ticket_id": tid,
        "rubric_scaffold": True,
        "cached": cached,
        "view_scope": "full" if is_manager else "own",
        **filtered,
    }


def score_ticket_route(request: Request, ticket_id: str, refresh: bool = False):
    org_id = auth.org_id_from_request(request)
    tid = _parse_ticket_id(ticket_id)
    viewer_id = auth.user_id_from_request(request)
    is_manager = auth.is_org_owner(request)

    ticket = ticket_ingest.get_ticket(tid, org_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    if ticket["status"] == "failed":
        raise HTTPException(
            status_code=400, detail="This ticket failed ingestion; there is nothing to score.",
        )
    if ticket["status"] != "ready":
        raise HTTPException(status_code=409, detail="This ticket is still processing.")
    if not ticket["messages"]:
        raise HTTPException(status_code=400, detail="This ticket has no messages to score.")

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

    prior = ticket_audit_store.fetch_latest(tid, org_id)
    if prior is not None:
        stored = prior["findings"]
        if refresh:
            if not org_features.features_for_org(org_id).get("enable_ticket_rescoring"):
                applog.event(
                    log, "ticket_rescore_blocked",
                    ticket_id=tid, score=prior.get("score"),
                )
                raise HTTPException(status_code=403, detail=_RESCORE_DENIED)
        else:
            applog.event(
                log, "ticket_audit_cache",
                result="HIT", ticket_id=tid, score=prior.get("score"),
            )
            payload = _payload(
                tid, stored, cached=True, viewer_user_id=viewer_id, is_manager=is_manager,
            )
            return _with_timeliness(payload, turns)

    try:
        rubric = ticket_rubric.ensure_ticket_rubric(org_id)
        result = ticket_scoring.score_ticket(turns, rubric["dimensions"])
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "ticket_scoring_failed", level=logging.ERROR,
            ticket_id=tid, error=applog.safe_exception_text(e),
        )
        sentry_report.capture_exception(e)
        raise HTTPException(status_code=502, detail="Ticket scoring failed.") from None

    try:
        ticket_audit_store.upsert(
            tid, org_id, result,
            requested_by=getattr(request.state, "user_id", None),
        )
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "ticket_audit_persist_failed", level=logging.ERROR,
            ticket_id=tid, error=applog.safe_exception_text(e),
        )
        sentry_report.capture_exception(e)
        raise HTTPException(status_code=502, detail="Ticket scoring failed.") from None

    applog.event(
        log, "ticket_scored",
        ticket_id=tid, score=result["score"], dimensions=len(result["findings"]),
        refresh=bool(refresh),
    )
    payload = _payload(
        tid, result, cached=False, viewer_user_id=viewer_id, is_manager=is_manager,
    )
    return _with_timeliness(payload, turns)


def register(app) -> None:
    app.add_api_route(
        "/api/tickets/{ticket_id}/score", score_ticket_route, methods=["POST"],
    )
