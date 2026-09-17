"""HTTP surface for Training drills.

GET /api/training — org-scoped. Owner/Manager may pass ?agent= for a
teammate; a member always sees their own drills. JWT org_id only.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from . import applog
from . import auth
from . import team_performance
from . import training

log = logging.getLogger("callproof.training")


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
    )
    return body


def register(app) -> None:
    app.add_api_route("/api/training", training_route, methods=["GET"])
