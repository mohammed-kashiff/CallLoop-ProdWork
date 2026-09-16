"""AC-72: a generous, in-process rate limiter for the expensive routes.

No Redis in this deployment (render.yaml: Render free plan, single
instance) — in-process is the right, proportionate choice, not a
shortcut. These tests are pure unit tests against the module directly;
the actual route wiring (ticket_score_api.score_ticket_route,
ticket_api.upload_ticket, api.upload/upload_batch) is exercised by
those routes' own existing test suites, which would start failing with
429s if the limit were ever set too tight for their fixtures."""

from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException


def test_allows_requests_under_the_limit():
    from backend import rate_limit

    bucket = str(uuid.uuid4())
    key = str(uuid.uuid4())
    for _ in range(5):
        rate_limit.enforce(bucket, key, limit=5, window_seconds=60)


def test_blocks_the_request_that_exceeds_the_limit():
    from backend import rate_limit

    bucket = str(uuid.uuid4())
    key = str(uuid.uuid4())
    for _ in range(3):
        rate_limit.enforce(bucket, key, limit=3, window_seconds=60)
    with pytest.raises(HTTPException) as exc_info:
        rate_limit.enforce(bucket, key, limit=3, window_seconds=60)
    assert exc_info.value.status_code == 429


def test_different_keys_have_independent_limits():
    """One org hammering a route must never degrade another org's access
    to it — the whole point of keying by org_id, not globally."""
    from backend import rate_limit

    bucket = str(uuid.uuid4())
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    for _ in range(3):
        rate_limit.enforce(bucket, org_a, limit=3, window_seconds=60)
    with pytest.raises(HTTPException):
        rate_limit.enforce(bucket, org_a, limit=3, window_seconds=60)
    # org_b is untouched by org_a's usage.
    rate_limit.enforce(bucket, org_b, limit=3, window_seconds=60)


def test_different_buckets_have_independent_limits():
    """Scoring and upload each get their own budget on the same org."""
    from backend import rate_limit

    key = str(uuid.uuid4())
    for _ in range(3):
        rate_limit.enforce("bucket-a", key, limit=3, window_seconds=60)
    with pytest.raises(HTTPException):
        rate_limit.enforce("bucket-a", key, limit=3, window_seconds=60)
    rate_limit.enforce("bucket-b", key, limit=3, window_seconds=60)


def test_window_expiry_frees_up_capacity():
    """A hit outside the trailing window no longer counts against the
    limit — proven by manipulating the module's own clock function rather
    than sleeping in a test."""
    from backend import rate_limit

    bucket = str(uuid.uuid4())
    key = str(uuid.uuid4())
    t = [1000.0]

    def _fake_monotonic():
        return t[0]

    import backend.rate_limit as rl_module

    orig = rl_module.time.monotonic
    rl_module.time.monotonic = _fake_monotonic
    try:
        rate_limit.enforce(bucket, key, limit=1, window_seconds=60)
        with pytest.raises(HTTPException):
            rate_limit.enforce(bucket, key, limit=1, window_seconds=60)
        t[0] += 61  # advance past the window
        rate_limit.enforce(bucket, key, limit=1, window_seconds=60)  # allowed again
    finally:
        rl_module.time.monotonic = orig


def test_blocked_request_logs_rate_limited_event(monkeypatch):
    from backend import rate_limit

    captured = []
    monkeypatch.setattr(
        "backend.rate_limit.applog.event",
        lambda logger, name, **fields: captured.append((name, fields)),
    )
    bucket = str(uuid.uuid4())
    key = str(uuid.uuid4())
    rate_limit.enforce(bucket, key, limit=1, window_seconds=60)
    with pytest.raises(HTTPException):
        rate_limit.enforce(bucket, key, limit=1, window_seconds=60)
    assert captured
    name, fields = captured[-1]
    assert name == "rate_limited"
    assert fields["bucket"] == bucket
    assert fields["key"] == key
