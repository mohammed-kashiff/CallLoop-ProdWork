"""Intercom Messenger Security (CallLoop's own support widget) — unrelated
to the OAuth data-ingestion integration. No live Intercom calls."""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient

from backend import intercom_widget


def test_is_configured_reflects_env_var(monkeypatch):
    monkeypatch.delenv("INTERCOM_MESSENGER_SECRET", raising=False)
    assert intercom_widget.is_configured() is False
    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    assert intercom_widget.is_configured() is True


def test_user_hash_matches_a_real_hmac_sha256(monkeypatch):
    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    expected = hmac.new(
        b"unified_secret_value", b"user-uuid-123", hashlib.sha256,
    ).hexdigest()
    assert intercom_widget.user_hash("user-uuid-123") == expected


def test_user_hash_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("INTERCOM_MESSENGER_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="intercom_messenger_not_configured"):
        intercom_widget.user_hash("user-uuid-123")


def test_user_hash_requires_a_user_id(monkeypatch):
    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    with pytest.raises(ValueError, match="user_id is required"):
        intercom_widget.user_hash("")


def test_user_hash_never_logged():
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
    assert r.json() == {"configured": False, "user_hash": None}


def test_widget_identity_returns_a_real_hash_when_configured(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.setenv("INTERCOM_MESSENGER_SECRET", "unified_secret_value")
    client = TestClient(app)
    uid = authorize(client, monkeypatch)
    r = client.get("/api/support/widget-identity")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["user_hash"] == intercom_widget.user_hash(uid)


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
