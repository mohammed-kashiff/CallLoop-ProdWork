"""AC-72: a generous, in-process rate limiter for the expensive routes
(ticket/call scoring, uploads) — real Claude cost per call, and no
app-level throttle existed anywhere before this (the only prior
"rate_limited" status in this codebase is for upstream Claude 429s, not
a limiter protecting our own endpoints).

In-process, not Redis-backed: render.yaml runs this service on Render's
free plan, a single instance with no horizontal scaling — there is
nothing to share state across, so a Redis-backed limiter would be
solving a problem this deployment doesn't have. Resets on every deploy,
which is fine for what this guards against (a runaway loop or an abuse
burst, not a long-term quota).

Scoped deliberately narrow and deliberately generous: this exists to
catch a genuine hammering pattern, not to throttle real usage. A false
positive here blocks a paying customer mid-workflow — this is the one
story in the Actor Identity & Audit Trail epic that changes real
behavior rather than just observing it.
"""

from __future__ import annotations

import logging
import threading
import time

from fastapi import HTTPException

from . import applog

log = logging.getLogger("callproof.rate_limit")

_lock = threading.Lock()
_hits: dict[tuple[str, str], list[float]] = {}


def _under_limit(bucket: str, key: str, *, limit: int, window_seconds: int) -> bool:
    """True and records a hit if (bucket, key) is still under its limit
    within the trailing window; False (no hit recorded) once it isn't."""
    now = time.monotonic()
    cutoff = now - window_seconds
    with _lock:
        hits = _hits.setdefault((bucket, key), [])
        while hits and hits[0] < cutoff:
            hits.pop(0)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True


def _reset_for_tests() -> None:
    """Test-only: clear all recorded hits. Module-level state would
    otherwise bleed across test functions that share an org_id (most
    fixtures reuse DEFAULT_ORG_ID), eventually tripping an unrelated
    test's request with a 429 purely from accumulated test volume."""
    with _lock:
        _hits.clear()


def enforce(bucket: str, key: str, *, limit: int, window_seconds: int) -> None:
    """Raise 429 if (bucket, key) is over its limit; no-op otherwise.
    key is typically an org_id — one org hammering a route shouldn't be
    able to degrade another org's access to it."""
    if _under_limit(bucket, key, limit=limit, window_seconds=window_seconds):
        return
    applog.event(
        log, "rate_limited", level=logging.WARNING,
        bucket=bucket, key=key, limit=limit, window_seconds=window_seconds,
    )
    raise HTTPException(
        status_code=429,
        detail="Too many requests. Please wait a moment and try again.",
    )
