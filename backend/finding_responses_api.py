"""HTTP for agent agree/dispute on a scored finding."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import audit_log
from . import auth
from . import finding_responses
from . import product_events
from . import rate_limit

log = logging.getLogger("callproof.findings")


class ResponseBody(BaseModel):
    channel: str
    dimension_id: str
    stance: str
    note: str | None = None
    call_id: int | None = None
    ticket_id: str | None = None


def put_response(request: Request, body: ResponseBody):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    rate_limit.enforce("finding_response", org_id, limit=60, window_seconds=300)
    try:
        row = finding_responses.upsert(
            org_id,
            viewer_user_id=viewer_id,
            channel=body.channel,
            dimension_id=body.dimension_id,
            stance=body.stance,
            note=body.note,
            call_id=body.call_id,
            ticket_id=body.ticket_id,
        )
    except PermissionError:
        raise HTTPException(
            status_code=403, detail="Only the scored agent can respond to this finding.",
        ) from None
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc) or "Not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not save this response.") from None
    event = "finding.disputed" if row["stance"] == "dispute" else "finding.agreed"
    product_name = "finding_disputed" if row["stance"] == "dispute" else "finding_agreed"
    audit_log.record(
        org_id, event,
        target_type="finding_response", target_id=row["id"],
        after={
            "channel": row["channel"],
            "dimension_id": row["dimension_id"],
            "stance": row["stance"],
            "has_note": bool(row.get("note")),
            "call_id": row.get("call_id"),
            "ticket_id": row.get("ticket_id"),
        },
    )
    product_events.track_event(
        org_id, viewer_id, product_name,
        {"channel": row["channel"], "dimension_id": row["dimension_id"], "stance": row["stance"]},
    )
    applog.event(
        log, product_name,
        channel=row["channel"],
        dim=row["dimension_id"],
        has_note=bool(row.get("note")),
    )
    return row


def list_responses(
    request: Request,
    stance: str | None = None,
    channel: str | None = None,
    call_id: int | None = None,
    ticket_id: str | None = None,
):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    want_disputes = (stance or "").strip().lower() == "dispute"
    if want_disputes:
        auth.require_owner_or_manager(request)
        try:
            rows = finding_responses.list_disputes(org_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "Could not load disputes.") from None
        return {"responses": rows}
    if not channel:
        raise HTTPException(status_code=400, detail="channel is required.")
    try:
        rows = finding_responses.list_for_source(
            org_id,
            viewer_user_id=viewer_id,
            is_manager=auth.is_owner_or_manager(request),
            channel=channel,
            call_id=call_id,
            ticket_id=ticket_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not load responses.") from None
    return {
        "responses": rows,
        "can_respond": finding_responses.viewer_can_respond(
            org_id,
            viewer_user_id=viewer_id,
            channel=channel,
            call_id=call_id,
            ticket_id=ticket_id,
        ),
    }


def register(app) -> None:
    app.add_api_route("/api/findings/response", put_response, methods=["PUT"])
    app.add_api_route("/api/findings/responses", list_responses, methods=["GET"])
