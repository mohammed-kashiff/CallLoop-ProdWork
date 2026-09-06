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
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

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
    record = captured[0]
    assert record.name == "callproof.usage.pyai"
    assert "event=api_consumption" in record.getMessage()
    assert "provider=pyai" in record.getMessage()
    # GET with no metering header — the aggregate estimate's "actions"
    # proxy only ever counts POSTs, so a bare poll must cost nothing.
    assert record.cost_usd == 0.0


def test_record_http_response_emits_api_consumption_for_anthropic(monkeypatch):
    monkeypatch.setattr(pyai_usage.db, "connection", _fake_db)
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

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
    record = captured[0]
    assert record.name == "callproof.usage.anthropic"
    assert "event=api_consumption" in record.getMessage()
    assert "provider=anthropic" in record.getMessage()
    assert record.cost_usd == pyai_usage.cost_estimate.rates()["claude_usd_per_hit"]


def test_record_http_response_uses_metered_units_for_pyai_cost(monkeypatch):
    monkeypatch.setattr(pyai_usage.db, "connection", _fake_db)
    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record)

    logger = logging.getLogger("callproof.usage.pyai")
    logger.setLevel(logging.INFO)
    handler = Capture()
    logger.addHandler(handler)
    try:
        pyai_usage.record_http_response(
            _FakeResponse(headers={"x-pyai-units": "3.5"}),
            provider="pyai",
            method="POST",
            url="https://pyai.example/hear",
        )
    finally:
        logger.removeHandler(handler)

    rate = pyai_usage.cost_estimate.rates()["pyai_usd_per_unit"]
    assert captured[0].cost_usd == round(3.5 * rate, 6)


def test_call_cost_usd_matrix():
    r = pyai_usage.cost_estimate.rates()
    # Claude: flat per hit, regardless of method or units.
    assert pyai_usage._call_cost_usd("anthropic", "POST", None) == round(r["claude_usd_per_hit"], 6)
    assert pyai_usage._call_cost_usd("anthropic", "GET", 5.0) == round(r["claude_usd_per_hit"], 6)
    # PyAI: metered units win when present, even on a GET (a poll can carry units too).
    assert pyai_usage._call_cost_usd("pyai", "GET", 2.0) == round(2.0 * r["pyai_usd_per_unit"], 6)
    # PyAI POST with no units — the coarse per-action proxy.
    assert pyai_usage._call_cost_usd("pyai", "POST", None) == round(r["pyai_usd_per_minute"], 6)
    # PyAI GET with no units — a plain poll, no proxy applies.
    assert pyai_usage._call_cost_usd("pyai", "GET", None) == 0.0
    # Unknown provider — never estimate blind.
    assert pyai_usage._call_cost_usd("mystery", "POST", 10.0) == 0.0


def test_record_http_response_never_raises_even_if_db_write_fails(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(pyai_usage.db, "connection", _boom)
    pyai_usage.record_http_response(_FakeResponse(), provider="pyai")
