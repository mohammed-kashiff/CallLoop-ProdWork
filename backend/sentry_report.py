"""Sentry for unhandled API failures. No PII, credentials, or DSN in logs."""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import HTTPException as FastAPIHTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import applog
from .config import skip_startup
from .org_ids import bound_org_id

log = logging.getLogger("callproof.sentry")

_DROP_HEADER_KEYS = frozenset({
    "authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-justcall-signature",
    "x-justcall-signature".title(),
    "x-webhook-signature",
})

_initialized = False


def dsn() -> str:
    return (os.getenv("SENTRY_DSN") or "").strip()


def environment() -> str:
    raw = (os.getenv("SENTRY_ENVIRONMENT") or "").strip()
    if raw:
        return raw
    if skip_startup() or os.getenv("PYTEST_CURRENT_TEST"):
        return "test"
    if (os.getenv("RENDER") or "").strip():
        return "production"
    return "local"


def init_sentry(*, transport=None) -> bool:
    """Bind the official FastAPI integration. No-op without DSN (except tests with transport)."""
    global _initialized
    if _initialized and transport is None:
        return True
    secret = dsn()
    if transport is None:
        if not secret:
            return False
        if skip_startup():
            return False
    import sentry_sdk
    from sentry_sdk.integrations.logging import LoggingIntegration

    sentry_sdk.init(
        dsn=secret or None,
        environment=environment(),
        send_default_pii=False,
        traces_sample_rate=0.0,
        profile_session_sample_rate=0.0,
        transport=transport,
        before_send=before_send,
        disabled_integrations=[LoggingIntegration],
    )
    _initialized = True
    applog.event(log, "sentry_ready", environment=environment())
    return True


def before_send(event: dict[str, Any] | None, hint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop 4xx, attach org_id, strip bodies/PII/secrets. Never log the event."""
    if not event:
        return None
    hint = hint or {}
    exc = _hint_exception(hint)
    if isinstance(exc, (FastAPIHTTPException, StarletteHTTPException)):
        status = int(getattr(exc, "status_code", 500) or 500)
        if status < 500:
            return None
    _scrub_event(event)
    oid = bound_org_id()
    if oid:
        tags = event.setdefault("tags", {})
        if isinstance(tags, dict):
            tags["org_id"] = oid
    event.pop("user", None)
    return event


def _hint_exception(hint: dict[str, Any]) -> BaseException | None:
    info = hint.get("exc_info")
    if isinstance(info, tuple) and len(info) >= 2:
        exc = info[1]
        if isinstance(exc, BaseException):
            return exc
    return None


def _scrub_event(event: dict[str, Any]) -> None:
    request = event.get("request")
    if isinstance(request, dict):
        request.pop("data", None)
        request.pop("cookies", None)
        request.pop("env", None)
        request.pop("query_string", None)
        headers = request.get("headers")
        if isinstance(headers, dict):
            cleaned = {}
            for key, value in headers.items():
                if str(key).lower() in _DROP_HEADER_KEYS:
                    cleaned[key] = "[REDACTED]"
                else:
                    cleaned[key] = _scrub_value(value)
            request["headers"] = cleaned
        event["request"] = request
    extra = event.get("extra")
    if isinstance(extra, dict):
        event["extra"] = _scrub_value(extra)
    for key in ("exception", "logentry", "message", "breadcrumbs"):
        if key in event:
            event[key] = _scrub_value(event[key])


def _scrub_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[REDACTED]"
    if isinstance(value, str):
        return applog.redact_line(value)
    if isinstance(value, dict):
        out = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if lowered in _DROP_HEADER_KEYS or lowered in {
                "authorization", "password", "secret", "token", "api_key",
                "api_secret", "dsn", "cookie", "email", "phone",
            }:
                out[key] = "[REDACTED]"
            else:
                out[key] = _scrub_value(inner, depth=depth + 1)
        return out
    if isinstance(value, list):
        return [_scrub_value(item, depth=depth + 1) for item in value[:50]]
    return value
