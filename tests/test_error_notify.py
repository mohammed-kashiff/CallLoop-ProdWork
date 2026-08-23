"""Error notification: 5xx only, cooldown, redaction, no secrets in payloads."""

from __future__ import annotations

import time

from backend import error_notify


def test_notifies_only_on_5xx():
    assert error_notify.should_notify_status(500) is True
    assert error_notify.should_notify_status(502) is True
    assert error_notify.should_notify_status(400) is False
    assert error_notify.should_notify_status(404) is False
    assert error_notify.should_notify_status(200) is False


def test_safe_text_redacts_keys():
    text = error_notify._safe_text("failed pyai_live_abcdefghijk and sk-ant-abcdefghijk")
    assert "pyai_live_" not in text
    assert "sk-ant-" not in text
    assert "[REDACTED]" in text


def test_cooldown_suppresses_duplicate(monkeypatch):
    monkeypatch.setenv("ERROR_NOTIFY_MIN_INTERVAL_SECONDS", "60")
    error_notify._last_sent.clear()
    key = error_notify._cooldown_key("POST", "/api/upload", 500)
    assert error_notify._in_cooldown(key) is False
    assert error_notify._in_cooldown(key) is True
    error_notify._last_sent[key] = time.monotonic() - 120
    assert error_notify._in_cooldown(key) is False


def test_notify_http_error_skips_4xx(monkeypatch):
    called = []
    monkeypatch.setattr(error_notify, "_send_desktop", lambda *a, **k: called.append("desktop"))
    monkeypatch.setenv("ERROR_NOTIFY_DESKTOP", "true")
    error_notify.notify_http_error(method="GET", path="/api/calls/1", status=404)
    time.sleep(0.05)
    assert called == []


def test_email_needs_valid_address(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("ERROR_NOTIFY_EMAIL", "not-an-email")
    addrs = error_notify.notify_emails()
    assert "not-an-email" not in addrs
    assert error_notify.DEFAULT_ERROR_NOTIFY_EMAILS[0] in addrs
    assert error_notify.DEFAULT_ERROR_NOTIFY_EMAILS[1] in addrs


def test_default_operator_emails(monkeypatch):
    monkeypatch.delenv("ERROR_NOTIFY_EMAIL", raising=False)
    addrs = error_notify.notify_emails()
    assert addrs == list(error_notify.DEFAULT_ERROR_NOTIFY_EMAILS)


def test_email_off_disables(monkeypatch):
    monkeypatch.setenv("ERROR_NOTIFY_EMAIL", "off")
    assert error_notify.notify_emails() == []
    assert error_notify.email_transport() is None


def test_email_address_only_uses_mail_app_on_mac(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("ERROR_NOTIFY_EMAIL", raising=False)
    monkeypatch.setattr(error_notify.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(error_notify, "smtp_available", lambda: False)
    assert error_notify.email_transport() == "mail.app"


def test_notify_http_error_sends_email_via_mail_app(monkeypatch):
    called = []
    monkeypatch.setenv("ERROR_NOTIFY_DESKTOP", "false")
    monkeypatch.setenv("ERROR_NOTIFY_WEBHOOK_URL", "")
    monkeypatch.setenv("ERROR_NOTIFY_EMAIL", "ops@example.com")
    monkeypatch.setenv("ERROR_NOTIFY_MIN_INTERVAL_SECONDS", "5")
    error_notify._last_sent.clear()
    monkeypatch.setattr(error_notify, "desktop_enabled", lambda: False)
    monkeypatch.setattr(error_notify, "email_transport", lambda: "mail.app")
    monkeypatch.setattr(
        error_notify,
        "_send_email",
        lambda subject, body: called.append((subject, body)),
    )
    error_notify.notify_http_error(
        method="GET",
        path="/api/calls",
        status=500,
        error="RuntimeError: boom",
    )
    deadline = time.time() + 2
    while not called and time.time() < deadline:
        time.sleep(0.02)
    assert called
    subject, body = called[0]
    assert "500" in subject
    assert "/api/calls" in body
    assert "boom" in body
    assert "pyai_live_" not in body


def test_notify_http_error_sends_desktop_on_500(monkeypatch):
    called = []
    monkeypatch.setenv("ERROR_NOTIFY_DESKTOP", "true")
    monkeypatch.setenv("ERROR_NOTIFY_WEBHOOK_URL", "")
    monkeypatch.setenv("ERROR_NOTIFY_EMAIL", "")
    monkeypatch.setenv("ERROR_NOTIFY_MIN_INTERVAL_SECONDS", "5")
    error_notify._last_sent.clear()
    monkeypatch.setattr(error_notify, "desktop_enabled", lambda: True)
    monkeypatch.setattr(error_notify, "email_transport", lambda: None)
    monkeypatch.setattr(
        error_notify,
        "_send_desktop",
        lambda title, subtitle, message: called.append((title, subtitle, message)),
    )
    error_notify.notify_http_error(
        method="POST",
        path="/api/upload",
        status=500,
        error="RuntimeError: boom",
        duration_ms=12.3,
    )
    deadline = time.time() + 2
    while not called and time.time() < deadline:
        time.sleep(0.02)
    assert called
    title, subtitle, message = called[0]
    assert title == "CallProof API error"
    assert "POST" in subtitle
    assert "/api/upload" in subtitle
    assert "500" in subtitle
    assert "boom" in message
