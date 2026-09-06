"""record_http_response must actually emit api_consumption — it used to
silently fail on every call because of a bare `import applog` inside a
package module (no top-level `applog` module exists; it's `backend.applog`),
swallowed by the surrounding try/except. Provider-scoped loggers let Better
Stack's `service` field split PyAI vs Claude/Anthropic volume."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from backend import pyai_usage


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        return None


@contextmanager
def _fake_db(*_a, **_k):
    yield _FakeConn()


class _FakeResponse:
    def __init__(self, status_code=200, headers=None, request=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.request = request


def test_record_http_response_emits_api_consumption_for_pyai(monkeypatch):
    monkeypatch.setattr(pyai_usage.db, "connection", _fake_db)
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append((record.name, record.getMessage()))

    logger = logging.getLogger("callproof.usage.pyai")
    logger.setLevel(logging.INFO)
    handler = Capture()
    logger.addHandler(handler)
    try:
        pyai_usage.record_http_response(
            _FakeResponse(), provider="pyai", method="GET", url="https://pyai.example/hear/123",
        )
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1
    name, message = captured[0]
    assert name == "callproof.usage.pyai"
    assert "event=api_consumption" in message
    assert "provider=pyai" in message


def test_record_http_response_emits_api_consumption_for_anthropic(monkeypatch):
    monkeypatch.setattr(pyai_usage.db, "connection", _fake_db)
    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append((record.name, record.getMessage()))

    logger = logging.getLogger("callproof.usage.anthropic")
    logger.setLevel(logging.INFO)
    handler = Capture()
    logger.addHandler(handler)
    try:
        pyai_usage.record_http_response(
            _FakeResponse(),
            provider="anthropic",
            method="POST",
            url="https://api.anthropic.com/v1/messages",
        )
    finally:
        logger.removeHandler(handler)

    assert len(captured) == 1
    name, message = captured[0]
    assert name == "callproof.usage.anthropic"
    assert "event=api_consumption" in message
    assert "provider=anthropic" in message


def test_record_http_response_never_raises_even_if_db_write_fails(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(pyai_usage.db, "connection", _boom)
    pyai_usage.record_http_response(_FakeResponse(), provider="pyai")
