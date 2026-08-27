"""
Local CallProof usage counters for outbound API calls.

PyAI does not expose a "requests used today" feed for the UI, so we record
each Hear/Recap (and optionally Claude) HTTP call CallProof makes, plus any
`x-pyai-units` metering header when present.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from . import db
from .org_ids import DEFAULT_ORG_ID, bound_org_id

log = logging.getLogger("callproof.usage")
_lock = threading.Lock()


def _conn():
    return db.connection()


def init_usage_db() -> None:
    """No-op: api_usage is created by Alembic."""
    return


def _utc_today_start() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT00:00:00+00:00")


def _normalize_path(url: str) -> str:
    try:
        parts = urlsplit(url)
        path = parts.path or "/"
        # Collapse job/call ids so grouping stays readable.
        bits = path.strip("/").split("/")
        out = []
        for b in bits:
            if b.startswith(("job_", "call_", "callproof", "org_", "key_", "req_")):
                out.append("{id}")
            elif len(b) > 24 and any(ch.isdigit() for ch in b):
                out.append("{id}")
            else:
                out.append(b)
        return "/" + "/".join(out) if out else path
    except Exception:  # noqa: BLE001
        return (url or "/")[:120]


def _parse_units(raw: str | None) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, dict):
        for key in ("units", "total", "audio_minutes", "minutes", "amount"):
            if key in data and isinstance(data[key], (int, float)):
                return float(data[key])
        # Sum numeric leaf values as a last resort.
        nums = [float(v) for v in data.values() if isinstance(v, (int, float))]
        if nums:
            return float(sum(nums))
    return None


def record_http_response(
    response,
    *,
    provider: str,
    method: str | None = None,
    url: str | None = None,
) -> None:
    """Persist one outbound API hit. Safe no-op on failure."""
    try:
        req = getattr(response, "request", None)
        method = (method or (req.method if req is not None else "GET") or "GET").upper()
        url = url or (str(req.url) if req is not None else "")
        path = _normalize_path(url)
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", {}) or {}
        units_raw = None
        for hk in ("x-pyai-units", "X-Pyai-Units", "x-units"):
            if hk in headers:
                units_raw = headers.get(hk)
                break
        units = _parse_units(units_raw)
        created = datetime.now(timezone.utc).isoformat()
        with _lock:
            with _conn() as c:
                c.execute(
                    """
                    INSERT INTO api_usage
                      (org_id, provider, method, path, status, units, units_raw, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        bound_org_id() or DEFAULT_ORG_ID,
                        provider,
                        method,
                        path,
                        int(status) if status is not None else None,
                        units,
                        (str(units_raw)[:240] if units_raw is not None else None),
                        created,
                    ),
                )
        try:
            import applog

            applog.event(
                log,
                "api_consumption",
                provider=provider,
                method=method,
                path=path,
                status=status,
                units=units if units is not None else "-",
            )
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        log.debug("api usage record skipped: %s", e)


def request(method: str, url: str, *, provider: str = "pyai", **kwargs):
    """httpx.request wrapper that records usage."""
    import httpx

    resp = httpx.request(method, url, **kwargs)
    record_http_response(resp, provider=provider, method=method, url=url)
    return resp


def get(url: str, *, provider: str = "pyai", **kwargs):
    return request("GET", url, provider=provider, **kwargs)


def post(url: str, *, provider: str = "pyai", **kwargs):
    return request("POST", url, provider=provider, **kwargs)


def usage_summary(since_iso: str | None = None, *, org_id: str | None = None) -> dict[str, Any]:
    """Aggregate hits since `since_iso` (default: UTC midnight)."""
    init_usage_db()
    since = since_iso or _utc_today_start()
    tenant = org_id or bound_org_id() or DEFAULT_ORG_ID
    with _lock:
        with _conn() as c:
            rows = c.execute(
                """
                SELECT provider, method, path, COUNT(*) AS hits,
                       COALESCE(SUM(units), 0) AS units,
                       SUM(CASE WHEN units IS NOT NULL THEN 1 ELSE 0 END) AS metered
                FROM api_usage
                WHERE org_id = %s AND created_at >= %s
                GROUP BY provider, method, path
                """,
                (tenant, since),
            ).fetchall()

    providers: dict[str, Any] = {}
    total_hits = 0
    total_units = 0.0
    total_actions = 0
    total_polls = 0
    top = []

    for r in rows:
        provider = r["provider"]
        method = (r["method"] or "").upper()
        path = r["path"] or ""
        hits = int(r["hits"] or 0)
        units = float(r["units"] or 0)
        is_poll = method == "GET" and (
            "/transcription/jobs/" in path or "/recap/calls/" in path
        )
        is_action = method == "POST"
        bucket = providers.setdefault(
            provider,
            {
                "hits": 0,
                "actions": 0,
                "polls": 0,
                "units": 0.0,
                "metered_responses": 0,
            },
        )
        bucket["hits"] += hits
        bucket["units"] = round(bucket["units"] + units, 4)
        bucket["metered_responses"] += int(r["metered"] or 0)
        if is_action:
            bucket["actions"] += hits
            total_actions += hits
        if is_poll:
            bucket["polls"] += hits
            total_polls += hits
        total_hits += hits
        total_units += units
        top.append(
            {
                "provider": provider,
                "method": method,
                "path": path,
                "hits": hits,
                "kind": "poll" if is_poll else ("action" if is_action else "other"),
            }
        )

    top.sort(key=lambda x: x["hits"], reverse=True)

    return {
        "since": since,
        "window": "utc_today",
        "total_hits": total_hits,
        "total_actions": total_actions,
        "total_polls": total_polls,
        "total_units": round(total_units, 4),
        "by_provider": providers,
        "top_paths": top[:12],
    }
