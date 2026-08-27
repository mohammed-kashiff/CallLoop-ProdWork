"""Sentry init: environment tag, org_id, no PII or credentials in events."""

from __future__ import annotations

from fastapi import HTTPException

from backend.org_ids import DEFAULT_ORG_ID, org_scope
from backend.sentry_report import before_send, environment, init_sentry


def test_environment_is_test_under_pytest():
    assert environment() == "test"


def test_environment_uses_explicit_env(monkeypatch):
    monkeypatch.setenv("SENTRY_ENVIRONMENT", "production")
    assert environment() == "production"


def test_init_noop_without_dsn_in_tests(monkeypatch):
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert init_sentry() is False


def test_before_send_drops_http_4xx():
    event = {"message": "not found"}
    hint = {"exc_info": (HTTPException, HTTPException(status_code=404), None)}
    assert before_send(event, hint) is None


def test_before_send_keeps_http_5xx():
    event = {"message": "boom"}
    hint = {"exc_info": (HTTPException, HTTPException(status_code=500), None)}
    out = before_send(event, hint)
    assert out is not None
    assert out["message"] == "boom"


def test_before_send_strips_body_and_authorization():
    event = {
        "request": {
            "method": "POST",
            "url": "https://example/api/upload",
            "data": {"justcall_api_secret": "jc_sec_should_not_leave"},
            "query_string": "token=abc",
            "headers": {
                "Authorization": "Bearer super-secret-token",
                "content-type": "application/json",
            },
        },
        "user": {"email": "ada@example.com", "ip_address": "1.2.3.4"},
        "extra": {"api_key": "pyai_live_abcdefghijk"},
    }
    out = before_send(event, {})
    assert out is not None
    assert "user" not in out
    req = out["request"]
    assert "data" not in req
    assert "query_string" not in req
    assert req["headers"]["Authorization"] == "[REDACTED]"
    blob = str(out)
    assert "super-secret-token" not in blob
    assert "jc_sec_should_not_leave" not in blob
    assert "ada@example.com" not in blob
    assert "pyai_live_" not in blob


def test_missing_pyai_key_is_not_treated_as_http_403():
    from backend.api import _upload_error_status

    msg = (
        "PYAI_API_KEY not configured. Set it on the host environment "
        "(live key with transcribe:jobs for CallProof)."
    )
    err = _upload_error_status(msg)
    assert err.status_code == 502
    hint = {"exc_info": (type(err), err, None)}
    assert before_send({"message": "x"}, hint) is not None


def test_before_send_tags_bound_org():
    event = {"message": "x"}
    with org_scope(DEFAULT_ORG_ID):
        out = before_send(event, {})
    assert out is not None
    assert out["tags"]["org_id"] == DEFAULT_ORG_ID
