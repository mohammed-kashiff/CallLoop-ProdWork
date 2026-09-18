"""HTTP surface for assignable Team Performance KPI targets.

GET /api/performance-kpis — catalog from the live rubric + stored targets.
PUT /api/performance-kpis — Owner/Manager set or clear one target.
JWT org_id only.
"""

from __future__ import annotations

import logging

from fastapi import Request
from pydantic import BaseModel

from . import applog
from . import auth
from . import performance_kpis

log = logging.getLogger("callproof.performance_kpis")


class PutKpiBody(BaseModel):
    channel: str
    dimension_id: str
    agent_user_id: str | None = None
    target: float | None = None


def get_kpis(request: Request):
    org_id = auth.org_id_from_request(request)
    viewer_id = auth.user_id_from_request(request)
    is_manager = auth.is_owner_or_manager(request)
    body = performance_kpis.catalog(
        org_id, viewer_user_id=viewer_id, is_manager=is_manager,
    )
    applog.event(
        log, "kpi_catalog_viewed",
        view_scope="team" if is_manager else "own",
        ticket_dims=len(body["tickets"]["dimensions"]),
        call_dims=len(body["calls"]["dimensions"]),
    )
    return body


def put_kpi(request: Request, body: PutKpiBody):
    auth.require_owner_or_manager(request)
    org_id = auth.org_id_from_request(request)
    result = performance_kpis.upsert(
        org_id,
        channel=body.channel,
        dimension_id=body.dimension_id,
        agent_user_id=body.agent_user_id,
        target=body.target,
    )
    applog.event(
        log, "kpi_target_updated",
        channel=result["channel"],
        dim=result["dimension_id"],
        cleared=result["target"] is None,
        has_agent=bool(result["agent_user_id"]),
    )
    return result


def register(app) -> None:
    app.add_api_route("/api/performance-kpis", get_kpis, methods=["GET"])
    app.add_api_route("/api/performance-kpis", put_kpi, methods=["PUT"])
