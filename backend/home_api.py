"""GET /api/home — self-scoped week for the customer app."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from . import applog
from . import auth
from . import home

log = logging.getLogger("callproof.home")


def home_route(request: Request):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    try:
        body = home.snapshot(org_id, viewer_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc) or "Could not load home.") from None
    applog.event(
        log, "home_viewed",
        flag_count=len(body["flags"]),
        drill_count=len(body["drills"]),
        ticket_count=len(body["tickets"]),
    )
    return body


def register(app) -> None:
    app.add_api_route("/api/home", home_route, methods=["GET"])
