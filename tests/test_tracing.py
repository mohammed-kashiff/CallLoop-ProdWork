"""Sentry spans must never break scoring; dimension hook opens/closes spans."""

from __future__ import annotations

import pytest

from backend import tracing


class FakeSpan:
    def __init__(self, op, name):
        self.op = op
        self.name = name
        self.data = {}
        self.status = None
        self.finished = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.finished = True
        return False

    def set_data(self, key, value):
        self.data[key] = value

    def set_status(self, status):
        self.status = status


def test_traces_sample_rate_defaults_to_one_tenth(monkeypatch):
    monkeypatch.delenv("SENTRY_TRACES_SAMPLE_RATE", raising=False)
    from backend.sentry_report import traces_sample_rate

    assert traces_sample_rate() == 0.1


def test_traces_sample_rate_clamps_and_falls_back(monkeypatch):
    from backend.sentry_report import traces_sample_rate

    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.2")
    assert traces_sample_rate() == 0.2
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "2")
    assert traces_sample_rate() == 1.0
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "-1")
    assert traces_sample_rate() == 0.0
    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "nope")
    assert traces_sample_rate() == 0.1


def test_init_sentry_passes_traces_sample_rate(monkeypatch):
    from backend import sentry_report

    monkeypatch.setenv("SENTRY_TRACES_SAMPLE_RATE", "0.2")
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.ingest.sentry.io/1")
    captured = {}

    def fake_init(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("sentry_sdk.init", fake_init)
    monkeypatch.setattr(sentry_report, "_initialized", False)
    try:
        assert sentry_report.init_sentry(transport=object()) is True
        assert captured["traces_sample_rate"] == 0.2
        assert captured["profile_session_sample_rate"] == 0.0
        assert captured["traces_sampler"] is sentry_report.traces_sampler
        assert captured["before_send_transaction"] is sentry_report.before_send
        assert sentry_report.traces_sampler({"asgi_scope": {"path": "/health"}}) == 0.0
        assert sentry_report.traces_sampler({"asgi_scope": {"path": "/api/calls/1/audit"}}) == 0.2
    finally:
        sentry_report._initialized = False


def test_span_still_runs_work_when_sentry_raises(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("sentry down")

    monkeypatch.setattr("sentry_sdk.start_span", boom)
    ran = []
    with tracing.span("task", "ticket.parse"):
        ran.append(1)
    assert ran == [1]


def test_span_reraises_the_work_exception(monkeypatch):
    monkeypatch.setattr("sentry_sdk.start_span", lambda **k: FakeSpan("task", "x"))
    with pytest.raises(RuntimeError, match="work failed"):
        with tracing.span("task", "x"):
            raise RuntimeError("work failed")


def test_dimension_event_opens_and_closes_without_reasoning(monkeypatch):
    opened: list[FakeSpan] = []

    def start_span(*, op, name, **_kw):
        span = FakeSpan(op, name)
        opened.append(span)
        return span

    monkeypatch.setattr("sentry_sdk.start_span", start_span)
    dim = {"id": "active_listening", "method": "llm"}
    tracing.dimension_event(
        dim, "started",
        {"method": "llm", "weight": 10, "reasoning": "customer said secret quote"},
    )
    assert len(opened) == 1
    assert opened[0].name == "criterion:active_listening"
    assert opened[0].op == "gen_ai.chat"
    assert opened[0].data.get("method") == "llm"
    assert "reasoning" not in opened[0].data
    assert "customer said secret quote" not in str(opened[0].data)

    tracing.dimension_event(
        dim, "succeeded",
        {"verdict": "pass", "reasoning": "still must not land on the span"},
    )
    assert opened[0].finished is True
    assert opened[0].status == "ok"
    assert opened[0].data.get("verdict") == "pass"
    assert "reasoning" not in opened[0].data


def test_dimension_event_never_raises(monkeypatch):
    def boom(**_kwargs):
        raise RuntimeError("cannot span")

    monkeypatch.setattr("sentry_sdk.start_span", boom)
    tracing.dimension_event({"id": "x"}, "started")
    tracing.dimension_event({"id": "x"}, "failed", {"error": "nope"})


def test_api_dimension_hook_also_emits_a_trace():
    src = (__import__("pathlib").Path(__file__).resolve().parents[1]
           / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def _on_dimension_event")
    end = src.index("\n    # One parallel wave", start)
    region = src[start:end]
    assert "call_trail.record" in region
    assert "tracing.dimension_event" in region
