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
ticket_scoring.score_ticket(). Response Timeliness (TA-13) is computed
deterministically from real message timestamps and appended to the
findings list separately — it is not part of score_ticket()'s weighted
score for v1.

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
from pydantic import BaseModel

from . import applog
from . import auth
from . import org_features
from . import sentry_report
from . import ticket_audit_store
from . import ticket_audit_summary
from . import ticket_ingest
from . import ticket_permissions
from . import ticket_rubric
from . import ticket_rubric_builder
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
    another agent's individual scores, even on a ticket they share.

    IN-12: audit_summary/top_strength/top_gap are computed here, after
    TA-12's own filtering, from the already-filtered findings — never
    persisted (see ticket_audit_summary.py's own docstring), and never
    computed from the unfiltered ticket, so a non-manager's summary
    reflects only what they personally contributed, same as their
    findings/spans already do.
    """
    filtered_findings = ticket_permissions.filter_findings_for_viewer(
        result.get("findings") or [], viewer_user_id=viewer_user_id, is_manager=is_manager,
    )
    filtered = {
        **result,
        "findings": filtered_findings,
        "spans": ticket_permissions.filter_spans_for_viewer(
            result.get("spans") or [], viewer_user_id=viewer_user_id, is_manager=is_manager,
        ),
        "top_strength": ticket_audit_summary.top_strength(filtered_findings),
        "top_gap": ticket_audit_summary.top_gap(filtered_findings),
        "audit_summary": ticket_audit_summary.generate_audit_summary(filtered_findings),
    }
    return {
        "ticket_id": tid,
        "cached": cached,
        "view_scope": "full" if is_manager else "own",
        **filtered,
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
