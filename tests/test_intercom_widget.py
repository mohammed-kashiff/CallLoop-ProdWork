"""Intercom Messenger Security via JWT (CallLoop's own support widget) —
unrelated to the OAuth data-ingestion integration. No live Intercom calls.

Rebuilt from an initial raw-HMAC ("user_hash") implementation after
testing showed Intercom's dashboard only marks the Messenger as "securely
installed" under their newer JWT-based scheme — the old hash still works
for backward compatibility, but doesn't satisfy Intercom's own check.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import intercom_widget


def test_is_configured_reflects_env_var(monkeypatch):
    monkeypatch.delenv("INTERCOM_MESSENGER_SECRET", raising=False)
    assert intercom_widget.is_configured() is False
    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    assert intercom_widget.is_configured() is True


def test_user_jwt_is_signed_hs256_and_decodes_with_the_secret(monkeypatch):
    import jwt as pyjwt

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    token = intercom_widget.user_jwt("user-uuid-123", "ada@example.com")
    header = pyjwt.get_unverified_header(token)
    assert header["alg"] == "HS256"
    payload = pyjwt.decode(token, "unified_secret_value", algorithms=["HS256"])
    assert payload["user_id"] == "user-uuid-123"
    assert payload["email"] == "ada@example.com"
    assert "exp" in payload and "iat" in payload


def test_user_jwt_omits_email_when_not_given(monkeypatch):
    import jwt as pyjwt

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    token = intercom_widget.user_jwt("user-uuid-123", None)
    payload = pyjwt.decode(token, "unified_secret_value", algorithms=["HS256"])
    assert "email" not in payload


def test_user_jwt_rejects_tampering_with_a_different_secret(monkeypatch):
    import jwt as pyjwt

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    token = intercom_widget.user_jwt("user-uuid-123")
    with pytest.raises(pyjwt.InvalidSignatureError):
        pyjwt.decode(token, "wrong_secret", algorithms=["HS256"])


def test_user_jwt_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("INTERCOM_MESSENGER_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="intercom_messenger_not_configured"):
        intercom_widget.user_jwt("user-uuid-123")


def test_user_jwt_requires_a_user_id(monkeypatch):
    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    with pytest.raises(ValueError, match="user_id is required"):
        intercom_widget.user_jwt("")


def test_secret_never_logged():
    """Source-level check, same discipline as org_vault/intercom_oauth:
    the secret must never end up in a log line."""
    from backend.paths import ROOT

    src = (ROOT / "backend" / "intercom_widget.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("log.") or "applog.event" in stripped:
            assert False, f"unexpected log line in intercom_widget.py: {stripped}"


# ── /api/support/widget-identity ─────────────────────────────────────────────


def test_widget_identity_returns_not_configured_when_secret_unset(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.delenv("INTERCOM_MESSENGER_SECRET", raising=False)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/support/widget-identity")
    assert r.status_code == 200
    assert r.json() == {"configured": False, "intercom_user_jwt": None}


def test_widget_identity_returns_a_real_jwt_when_configured(monkeypatch):
    import jwt as pyjwt

    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    client = TestClient(app)
    uid = authorize(client, monkeypatch)
    r = client.get("/api/support/widget-identity")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    payload = pyjwt.decode(
        body["intercom_user_jwt"], "unified_secret_value", algorithms=["HS256"],
    )
    assert payload["user_id"] == uid


def test_widget_identity_requires_auth():
    from backend.api import app

    client = TestClient(app)  # no authorize()
    r = client.get("/api/support/widget-identity")
    assert r.status_code == 401


def test_widget_identity_never_echoes_the_secret(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/support/widget-identity")
    assert "unified_secret_value" not in r.text
