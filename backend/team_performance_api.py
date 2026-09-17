"""HTTP surface for the Team Performance Dashboard.

GET /api/team-performance — org-scoped rollup. Owner/Manager see the
whole team; a member sees only their own row. Ticket and call Top
Strength/Gap are the mode of ticket_audit_summary pickers across the
window. Coverage is scored vs total in the window.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from . import applog
from . import auth
from . import team_performance

log = logging.getLogger("callproof.team_performance")


def team_performance_route(request: Request, days: int = 30):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    is_manager = auth.is_owner_or_manager(request)
    n = team_performance.clamp_days(days)
    try:
        body = team_performance.snapshot(
            org_id,
            viewer_user_id=viewer_id,
            is_manager=is_manager,
            days=n,
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="Could not load team performance.") from None
    applog.event(
        log, "team_performance_viewed",
        view_scope=body["view_scope"],
        days=n,
        agent_count=len(body["agents"]),
    )
    return body


def register(app) -> None:
    app.add_api_route("/api/team-performance", team_performance_route, methods=["GET"])
