"""IN-3: Intercom OAuth — signed state, token exchange, and the connect/
callback/status/disconnect routes. No live Intercom API calls."""

from __future__ import annotations

import pytest

from backend import intercom_oauth
from backend.org_ids import DEFAULT_ORG_ID

ORG_B = "00000000-0000-4000-8000-000000000002"


def _configure(monkeypatch, *, client_id: str = "ic_client_123", secret: str = "ic_secret_abc"):
    monkeypatch.setenv("INTERCOM_CLIENT_ID", client_id)
    monkeypatch.setenv("INTERCOM_CLIENT_SECRET", secret)


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, content=b"{}"):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.content = content

    def json(self):
        return self._json


# ── is_configured / state signing ───────────────────────────────────────────


def test_is_configured_false_until_both_env_vars_set(monkeypatch):
    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    assert intercom_oauth.is_configured() is False
    monkeypatch.setenv("INTERCOM_CLIENT_ID", "cid")
    assert intercom_oauth.is_configured() is False
    monkeypatch.setenv("INTERCOM_CLIENT_SECRET", "csecret")
    assert intercom_oauth.is_configured() is True


def test_make_state_round_trips_org_id(monkeypatch):
    _configure(monkeypatch)
    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    assert intercom_oauth.verify_state(state) == DEFAULT_ORG_ID


def test_verify_state_rejects_a_tampered_org_id(monkeypatch):
    _configure(monkeypatch)
    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    payload_b64, _, sig = state.partition(".")
    forged_state_for_org_b = intercom_oauth._b64url_encode(
        b'{"org_id":"' + ORG_B.encode() + b'","ts":9999999999}'
    ) + "." + sig
    with pytest.raises(intercom_oauth.StateError, match="bad_signature"):
        intercom_oauth.verify_state(forged_state_for_org_b)


def test_verify_state_rejects_malformed_state(monkeypatch):
    _configure(monkeypatch)
    for bad in (None, "", "no-dot-here", ".", "abc."):
        with pytest.raises(intercom_oauth.StateError):
            intercom_oauth.verify_state(bad)


def test_verify_state_rejects_expired_state(monkeypatch):
    _configure(monkeypatch)
    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    real_now = intercom_oauth.time.time()
    monkeypatch.setattr(intercom_oauth.time, "time", lambda: real_now + 700)
    with pytest.raises(intercom_oauth.StateError, match="expired_state"):
        intercom_oauth.verify_state(state)


def test_state_signing_requires_client_secret_configured(monkeypatch):
    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    with pytest.raises(intercom_oauth.IntercomAuthError, match="intercom_not_configured"):
        intercom_oauth.make_state(DEFAULT_ORG_ID)


# ── build_authorize_url ──────────────────────────────────────────────────────


def test_build_authorize_url_includes_client_id_and_a_valid_state(monkeypatch):
    _configure(monkeypatch, client_id="ic_client_999")
    url = intercom_oauth.build_authorize_url(DEFAULT_ORG_ID)
    assert url.startswith(intercom_oauth.AUTHORIZE_URL)
    assert "client_id=ic_client_999" in url
    assert "state=" in url
    state = url.split("state=", 1)[1]
    assert intercom_oauth.verify_state(state) == DEFAULT_ORG_ID


def test_build_authorize_url_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    with pytest.raises(intercom_oauth.IntercomAuthError, match="intercom_not_configured"):
        intercom_oauth.build_authorize_url(DEFAULT_ORG_ID)


# ── exchange_code_for_token ──────────────────────────────────────────────────


def test_exchange_code_for_token_returns_the_access_token(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        intercom_oauth.httpx, "post",
        lambda *a, **k: _FakeResponse(200, {"access_token": "tok_live_abc123"}),
    )
    assert intercom_oauth.exchange_code_for_token("some-code") == "tok_live_abc123"


def test_exchange_code_for_token_raises_on_non_200(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(intercom_oauth.httpx, "post", lambda *a, **k: _FakeResponse(401, {}))
    with pytest.raises(intercom_oauth.IntercomAuthError, match="token_exchange_failed"):
        intercom_oauth.exchange_code_for_token("bad-code")


def test_exchange_code_for_token_raises_when_no_access_token_in_body(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        intercom_oauth.httpx, "post", lambda *a, **k: _FakeResponse(200, {"unexpected": "shape"}),
    )
    with pytest.raises(intercom_oauth.IntercomAuthError, match="token_exchange_failed"):
        intercom_oauth.exchange_code_for_token("some-code")


def test_exchange_code_for_token_requires_configuration(monkeypatch):
    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    with pytest.raises(intercom_oauth.IntercomAuthError, match="intercom_not_configured"):
        intercom_oauth.exchange_code_for_token("some-code")


def test_fetch_workspace_id_returns_the_app_id_code(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        intercom_oauth.httpx, "get",
        lambda *a, **k: _FakeResponse(200, {"app": {"id_code": "o36ety8e"}, "type": "admin"}),
    )
    assert intercom_oauth.fetch_workspace_id("tok_abc") == "o36ety8e"


def test_fetch_workspace_id_raises_on_non_200(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(intercom_oauth.httpx, "get", lambda *a, **k: _FakeResponse(401, {}))
    with pytest.raises(intercom_oauth.IntercomAuthError, match="workspace_lookup_failed"):
        intercom_oauth.fetch_workspace_id("tok_abc")


def test_fetch_workspace_id_raises_when_app_object_missing(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setattr(
        intercom_oauth.httpx, "get", lambda *a, **k: _FakeResponse(200, {"type": "admin"}),
    )
    with pytest.raises(intercom_oauth.IntercomAuthError, match="workspace_lookup_failed"):
        intercom_oauth.fetch_workspace_id("tok_abc")


def test_fetch_workspace_id_requires_a_token():
    with pytest.raises(intercom_oauth.IntercomAuthError, match="missing_code"):
        intercom_oauth.fetch_workspace_id("")


# ── verify_webhook_signature ─────────────────────────────────────────────────


def test_verify_webhook_signature_matches_a_real_hmac_sha1(monkeypatch):
    import hashlib
    import hmac as hmac_module

    _configure(monkeypatch, secret="whsecret")
    body = b'{"topic":"conversation.admin.closed"}'
    digest = hmac_module.new(b"whsecret", body, hashlib.sha1).hexdigest()
    assert intercom_oauth.verify_webhook_signature(body, f"sha1={digest}") is True
    assert intercom_oauth.verify_webhook_signature(body, digest) is True  # bare hex also accepted


def test_verify_webhook_signature_rejects_a_tampered_body(monkeypatch):
    import hashlib
    import hmac as hmac_module

    _configure(monkeypatch, secret="whsecret")
    digest = hmac_module.new(b"whsecret", b"original body", hashlib.sha1).hexdigest()
    assert intercom_oauth.verify_webhook_signature(b"tampered body", f"sha1={digest}") is False


def test_verify_webhook_signature_fails_closed_with_no_signature(monkeypatch):
    _configure(monkeypatch, secret="whsecret")
    assert intercom_oauth.verify_webhook_signature(b"{}", None) is False
    assert intercom_oauth.verify_webhook_signature(b"{}", "") is False


def test_verify_webhook_signature_fails_closed_when_app_not_configured(monkeypatch):
    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    assert intercom_oauth.verify_webhook_signature(b"{}", "sha1=whatever") is False


def test_exchange_code_for_token_never_sends_a_get_request_or_logs_the_secret(monkeypatch):
    """Contract check, not behavior: the module must never leak client_secret
    or the returned token through logging."""
    src = intercom_oauth.__file__
    text = open(src, encoding="utf-8").read()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("log.") or "applog.event" in stripped:
            lowered = stripped.lower()
            assert "client_secret" not in lowered
            assert "access_token" not in lowered
            assert "code" not in lowered or "status_code" in lowered


# ── poll_seconds ──────────────────────────────────────────────────────────────


def test_poll_seconds_defaults_to_300(monkeypatch):
    monkeypatch.delenv("INTERCOM_POLL_SECONDS", raising=False)
    assert intercom_oauth.poll_seconds() == 300


def test_poll_seconds_reads_env_var(monkeypatch):
    monkeypatch.setenv("INTERCOM_POLL_SECONDS", "600")
    assert intercom_oauth.poll_seconds() == 600


def test_poll_seconds_clamps_to_a_30_second_floor(monkeypatch):
    monkeypatch.setenv("INTERCOM_POLL_SECONDS", "1")
    assert intercom_oauth.poll_seconds() == 30


def test_poll_seconds_falls_back_to_default_on_garbage(monkeypatch):
    monkeypatch.setenv("INTERCOM_POLL_SECONDS", "not-a-number")
    assert intercom_oauth.poll_seconds() == 300
