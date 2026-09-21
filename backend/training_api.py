"""HTTP surface for Training drills.

GET /api/training — org-scoped. Owner/Manager may pass ?agent= for a
teammate; a member always sees their own drills. JWT org_id only.
POST /api/training/assignments — owner/manager assign.
POST /api/training/assignments/{assignment_id}/complete — assignee only.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, Request
from pydantic import BaseModel

from . import applog
from . import audit_log
from . import auth
from . import product_events
from . import rate_limit
from . import team_performance
from . import training

log = logging.getLogger("callproof.training")


class AssignBody(BaseModel):
    agent: str
    channel: str
    dimension_id: str
    call_id: int | None = None
    ticket_id: str | None = None


class CompleteBody(BaseModel):
    reply: str | None = None


def training_route(
    request: Request,
    days: int = 30,
    channel: str | None = None,
    dim: str | None = None,
    agent: str | None = None,
):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    is_manager = auth.is_owner_or_manager(request)
    n = team_performance.clamp_days(days)
    try:
        ch = training.parse_channel(channel)
        dimension = training.parse_dim(dim)
        body = training.snapshot(
            org_id,
            viewer_user_id=viewer_id,
            is_manager=is_manager,
            days=n,
            channel=ch,
            dim=dimension,
            agent=agent if is_manager else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not load training.") from None
    applog.event(
        log, "training_viewed",
        view_scope=body["view_scope"],
        days=n,
        channel=ch or "",
        dim=dimension or "",
        drill_count=len(body["drills"]),
        assignment_count=len(body.get("assignments") or []),
    )
    return body


def assign_route(request: Request, body: AssignBody):
    auth.require_owner_or_manager(request)
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    rate_limit.enforce("training_assign", org_id, limit=60, window_seconds=300)
    try:
        row = training.assign(
            org_id,
            assigned_by=viewer_id,
            agent=body.agent,
            channel=body.channel,
            dimension_id=body.dimension_id,
            call_id=body.call_id,
            ticket_id=body.ticket_id,
        )
    except training.DuplicateOpenAssignment:
        raise HTTPException(
            status_code=409, detail="This drill is already assigned and still open.",
        ) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not assign this drill.") from None
    audit_log.record(
        org_id, "training.assigned",
        target_type="training_assignment", target_id=row["id"],
        after={
            "assignee_user_id": row["assignee_user_id"],
            "channel": row["channel"],
            "dimension_id": row["dimension_id"],
            "status": row["status"],
        },
    )
    product_events.track_event(
        org_id, viewer_id, "training_assigned",
        {"channel": row["channel"], "dimension_id": row["dimension_id"]},
    )
    applog.event(
        log, "training_assigned",
        assignment_id=row["id"],
        channel=row["channel"],
        dim=row["dimension_id"],
    )
    return row


def complete_route(request: Request, assignment_id: uuid.UUID, body: CompleteBody | None = None):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    payload = body or CompleteBody()
    try:
        row = training.complete(
            org_id,
            str(assignment_id),
            viewer_user_id=viewer_id,
            reply=payload.reply,
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Only the assigned agent can complete this drill.") from None
    except LookupError:
        raise HTTPException(status_code=404, detail="Assignment not found.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not complete this drill.") from None
    audit_log.record(
        org_id, "training.completed",
        target_type="training_assignment", target_id=row["id"],
        after={"status": row["status"], "has_reply": bool(row.get("reply"))},
    )
    product_events.track_event(
        org_id, viewer_id, "training_completed",
        {"channel": row["channel"], "has_reply": bool(row.get("reply"))},
    )
    applog.event(
        log, "training_completed",
        assignment_id=row["id"],
        has_reply=bool(row.get("reply")),
    )
    return row


def register(app) -> None:
    app.add_api_route("/api/training", training_route, methods=["GET"])
    app.add_api_route("/api/training/assignments", assign_route, methods=["POST"])
    app.add_api_route(
        "/api/training/assignments/{assignment_id}/complete",
        complete_route, methods=["POST"],
    )
