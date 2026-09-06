"""Log redaction must not keep header values or API keys."""

from __future__ import annotations

import io
import logging

from backend.applog import redact_line, safe_exception_text


def test_safe_exception_text_strips_illegal_header_value():
    class LocalProtocolError(Exception):
        pass

    exc = LocalProtocolError("Illegal header value b'sk-ant-fakekeyvaluexxxx\\n'")
    text = safe_exception_text(exc)
    assert "sk-ant-" not in text
    assert "fakekeyvaluexxxx" not in text
    assert "illegal HTTP header value" in text
    assert "LocalProtocolError" in text


def test_redact_line_covers_header_blob_and_key_prefix():
    line = "claude attempt 1/4 exception: LocalProtocolError: Illegal header value b'sk-ant-zzzzzzzzzzzzzzzz'"
    out = redact_line(line)
    assert "sk-ant-" not in out
    assert "zzzzzzzzzzzzzzzz" not in out
    assert "[REDACTED]" in out


def test_redact_line_covers_justcall_secret_fields():
    line = "justcall_api_secret=jc_sec_should_not_remain api_key=abc"
    out = redact_line(line)
    assert "jc_sec_should_not_remain" not in out
    assert "[REDACTED]" in out


def test_setup_logging_attaches_the_redact_filter_to_the_callproof_logger():
    """AC-47: the filter must live on the logger itself, not just the file
    handler — that's what makes it apply to console output too (any
    handler downstream via propagation), not just the rotating file."""
    from backend import applog

    applog.setup_logging()
    parent = logging.getLogger("callproof")
    assert any(isinstance(f, applog._RedactFilter) for f in parent.filters)


def test_event_includes_request_id_only_when_one_is_bound():
    from backend import applog

    logger = logging.getLogger("test_ac48_event_request_id")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        applog.event(logger, "no_request_bound")
        assert "request_id=" not in buf.getvalue()

        token = applog.bind_request_id("abc123")
        try:
            applog.event(logger, "request_bound")
        finally:
            applog.reset_request_id(token)
        assert "request_id=abc123" in buf.getvalue()
    finally:
        logger.removeHandler(handler)


def test_request_id_middleware_binds_a_fresh_id_per_request():
    """Unit-tests the middleware directly against a minimal ASGI inner app,
    rather than registering a probe route on the shared api.app singleton
    (which would permanently mutate it for the rest of the test session)."""
    import asyncio

    from backend import applog

    seen: list[str | None] = []

    async def inner_app(scope, receive, send):
        seen.append(applog.bound_request_id())

    middleware = applog.RequestIdMiddleware(inner_app)

    async def _one_request():
        await middleware({"type": "http"}, None, None)

    asyncio.run(_one_request())
    asyncio.run(_one_request())

    assert len(seen) == 2
    assert seen[0] is not None
    assert seen[1] is not None
    assert seen[0] != seen[1]
    # bound only for the request's lifetime — nothing leaks after it ends
    assert applog.bound_request_id() is None


def test_request_id_middleware_ignores_non_http_scopes():
    """Lifespan/websocket scopes must pass through untouched — no id bound."""
    import asyncio

    from backend import applog

    called = []

    async def inner_app(scope, receive, send):
        called.append(applog.bound_request_id())

    middleware = applog.RequestIdMiddleware(inner_app)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert called == [None]


def test_request_id_is_not_bound_outside_a_request():
    from backend import applog

    assert applog.bound_request_id() is None


def test_a_filter_on_the_logger_redacts_before_any_handler_sees_it():
    """Proves the actual mechanism AC-47 relies on, isolated from the
    global callproof logger's already-configured state: a Filter attached
    to a Logger (not a Handler) mutates the record before it reaches ANY
    handler — including a plain console-style StreamHandler, simulating
    the root logger's console output that callproof.* records propagate
    to. This was the gap: the redact filter used to live only on the file
    handler, so this same message would have reached console unredacted."""
    from backend.applog import _RedactFilter

    logger = logging.getLogger("test_ac47_console_redaction")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addFilter(_RedactFilter())

    console = io.StringIO()
    handler = logging.StreamHandler(console)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    try:
        logger.info("token leaked: Bearer sk-ant-totallysecretvalue1234")
    finally:
        logger.removeHandler(handler)

    output = console.getvalue()
    assert "sk-ant-totallysecretvalue1234" not in output
    assert "[REDACTED]" in output
