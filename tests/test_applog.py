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


def test_org_tagged_error_puts_org_name_inside_the_promoted_error_field():
    from backend.applog import org_tagged_error, org_tagged_exception

    out = org_tagged_error(
        "JustCall is not connected. Save the API key and secret on the Integrations page.",
        org_name="Acme Support",
        org_id="00000000-0000-4000-8000-000000000001",
    )
    assert out.startswith("[org=Acme Support] ")
    assert "JustCall is not connected" in out
    assert "00000000-0000-4000-8000-000000000001" not in out  # name wins

    fallback = org_tagged_error("boom", org_name=None, org_id="org-uuid-1")
    assert fallback == "[org=org-uuid-1] boom"

    tagged = org_tagged_exception(
        RuntimeError("JustCall is not connected. Save the API key and secret on the Integrations page."),
        org_name="Acme Support",
        org_id="00000000-0000-4000-8000-000000000001",
    )
    assert tagged == (
        "[org=Acme Support] JustCall is not connected. "
        "Save the API key and secret on the Integrations page."
    )
    assert "RuntimeError" not in tagged


def test_org_tagged_error_redacts_secrets_in_the_message():
    from backend.applog import org_tagged_error

    out = org_tagged_error(
        "Bearer sk-ant-totallysecretvalue1234 exploded",
        org_name="Acme",
    )
    assert "sk-ant-totallysecretvalue1234" not in out
    assert out.startswith("[org=Acme] ")


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


def test_event_attaches_fields_as_extra_for_structured_ingestion():
    """Fields must land as real record attributes (extra=), not just text
    inside message — that's what lets Better Stack group/filter on them
    (e.g. `path`) the same way it now does for `service`."""
    from backend import applog

    logger = logging.getLogger("test_event_extra")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = Capture()
    logger.addHandler(handler)
    try:
        applog.event(logger, "http_request", method="GET", path="/api/calls", status=200)
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1
    record = captured[0]
    assert record.event_name == "http_request"
    assert record.method == "GET"
    assert record.path == "/api/calls"
    assert record.status == 200
    assert "event=http_request" in record.getMessage()


def test_event_redacts_secrets_in_extra_the_same_as_in_the_text_line():
    from backend import applog

    logger = logging.getLogger("test_event_extra_redaction")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = Capture()
    logger.addHandler(handler)
    try:
        applog.event(logger, "leaky", error="Bearer sk-ant-totallysecretvalue1234")
    finally:
        logger.removeHandler(handler)

    record = captured[0]
    assert "sk-ant-totallysecretvalue1234" not in record.error
    assert "[REDACTED]" in record.error
    assert "sk-ant-totallysecretvalue1234" not in record.getMessage()


def test_event_drops_fields_colliding_with_reserved_logrecord_attrs():
    """A field named e.g. `module` would raise inside logging.log(extra=...)
    if passed through unfiltered — event() must survive that, not crash
    the caller."""
    from backend import applog

    logger = logging.getLogger("test_event_reserved_collision")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    handler = Capture()
    logger.addHandler(handler)
    try:
        applog.event(logger, "weird", module="should_not_crash", status=200)
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1
    assert captured[0].status == 200
    assert "module=should_not_crash" in captured[0].getMessage()


def test_redact_line_covers_betterstack_source_token():
    line = "betterstack_source_token=bst_should_not_remain source_token=abc"
    out = redact_line(line)
    assert "bst_should_not_remain" not in out
    assert "[REDACTED]" in out


def test_attach_logtail_is_noop_without_a_token(monkeypatch):
    from backend import applog

    monkeypatch.delenv("BETTERSTACK_SOURCE_TOKEN", raising=False)
    monkeypatch.delenv("LOGTAIL_SOURCE_TOKEN", raising=False)
    logger = logging.getLogger("test_logtail_noop")
    logger.handlers.clear()
    assert applog.attach_logtail_handler(logger) is False
    assert not any(isinstance(h, applog._BetterStackHandler) for h in logger.handlers)


def test_attach_logtail_is_additive_and_does_not_log_the_token(monkeypatch):
    from backend import applog

    created: dict = {}

    class FakeLogtail(logging.Handler):
        def __init__(self, source_token=None, host=None, **_kw):
            super().__init__()
            created["has_token"] = bool(source_token)
            created["host"] = host
            created["messages"] = []

        def emit(self, record):
            created["messages"].append(record.getMessage())

    import sys
    import types

    fake = types.ModuleType("logtail")
    fake.LogtailHandler = FakeLogtail
    monkeypatch.setitem(sys.modules, "logtail", fake)
    monkeypatch.setenv("BETTERSTACK_SOURCE_TOKEN", "tok_must_not_appear_in_logs")
    monkeypatch.delenv("BETTERSTACK_INGESTING_HOST", raising=False)

    logger = logging.getLogger("test_logtail_attach")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    assert applog.attach_logtail_handler(logger) is True
    assert created["has_token"] is True
    assert any(isinstance(h, applog._BetterStackHandler) for h in logger.handlers)
    # second attach is idempotent
    assert applog.attach_logtail_handler(logger) is True
    assert sum(1 for h in logger.handlers if isinstance(h, applog._BetterStackHandler)) == 1

    applog.event(logger, "hello_sink")
    blob = " ".join(created["messages"])
    assert "event=hello_sink" in blob
    assert "tok_must_not_appear_in_logs" not in blob


def test_betterstack_handler_stamps_service_from_logger_name():
    """Better Stack's dashboard groups by a top-level `service` field, but
    logtail-python only nests the logger name under context.runtime —
    unusable for Metrics grouping. The handler must promote it to a plain
    record attribute so it lands as a top-level field on ingest."""
    from backend import applog

    captured: dict = {}

    class Capture(logging.Handler):
        def emit(self, record):
            captured["service"] = getattr(record, "service", None)

    wrapped = applog._BetterStackHandler(Capture())
    record = logging.LogRecord(
        "callproof.ticket_scoring", logging.INFO, __file__, 1, "event=x", None, None,
    )
    wrapped.emit(record)
    assert captured["service"] == "callproof.ticket_scoring"


def test_betterstack_handler_does_not_override_an_explicit_service():
    from backend import applog

    captured: dict = {}

    class Capture(logging.Handler):
        def emit(self, record):
            captured["service"] = getattr(record, "service", None)

    wrapped = applog._BetterStackHandler(Capture())
    record = logging.LogRecord(
        "callproof.api", logging.INFO, __file__, 1, "event=x", None, None,
    )
    record.service = "explicit_value"
    wrapped.emit(record)
    assert captured["service"] == "explicit_value"


def test_betterstack_handler_emit_never_raises():
    from backend import applog

    class Boom(logging.Handler):
        def emit(self, record):
            raise RuntimeError("ingest host down")

    wrapped = applog._BetterStackHandler(Boom())
    record = logging.LogRecord(
        "callproof.test", logging.INFO, __file__, 1, "event=x", None, None,
    )
    wrapped.emit(record)


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
