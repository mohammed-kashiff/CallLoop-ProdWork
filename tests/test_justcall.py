"""JustCall helpers and webhook routes. No live JustCall API calls."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.justcall import extract_completed_id, identity_for, verify_webhook_signature


def test_identity_is_namespaced():
    assert identity_for("12345") == "justcall:12345"


def test_extract_completed_id_from_webhook():
    assert extract_completed_id({
        "type": "call.completed",
        "data": {"id": 99},
    }) == "99"


def test_extract_completed_id_ignores_validation_ping():
    assert extract_completed_id({"type": "url.validation"}) is None
    assert extract_completed_id({"type": "call.initiated", "data": {"id": 1}}) is None


def test_extract_completed_id_from_list_row():
    assert extract_completed_id({"id": "abc-1", "agent_name": "Ada"}) == "abc-1"


def test_webhook_signature_optional_when_secret_unset(monkeypatch):
    monkeypatch.delenv("JUSTCALL_WEBHOOK_SECRET", raising=False)
    assert verify_webhook_signature(b"{}", None) is True


def test_webhook_signature_required_when_secret_set(monkeypatch):
    monkeypatch.setenv("JUSTCALL_WEBHOOK_SECRET", "s3cret")
    assert verify_webhook_signature(b"{}", None) is False
    import hashlib
    import hmac
    sig = hmac.new(b"s3cret", b"{}", hashlib.sha256).hexdigest()
    assert verify_webhook_signature(b"{}", sig) is True
    assert verify_webhook_signature(b"{}", "sha256=" + sig) is True


def test_webhook_validation_returns_200():
    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/integrations/justcall/webhook", json={"type": "url.validation"})
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_status_never_echoes_secrets():
    from backend.api import app

    client = TestClient(app)
    r = client.get("/api/integrations/justcall")
    assert r.status_code == 200
    body = r.json()
    assert "configured" in body
    blob = str(body).lower()
    assert "justcall_api" not in blob
    assert "api_secret" not in blob
    assert ":" not in str(body.get("configured"))


def test_update_justcall_keys_persist_and_do_not_echo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    env_path = tmp_path / ".env"
    env_path.write_text("AUDIT_MODE=hybrid\n", encoding="utf-8")
    monkeypatch.setattr("backend.api.ENV_FILE", str(env_path))
    monkeypatch.delenv("JUSTCALL_API_KEY", raising=False)
    monkeypatch.delenv("JUSTCALL_API_SECRET", raising=False)
    key = "jc_key_" + ("a" * 12)
    secret = "jc_sec_" + ("b" * 12)
    client = TestClient(app)
    r = client.post(
        "/api/keys",
        json={"justcall_api_key": key, "justcall_api_secret": secret},
    )
    assert r.status_code == 200, r.text
    assert key not in r.text
    assert secret not in r.text
    body = r.json()
    assert body["ok"] is True
    assert "justcall" in body["updated"]
    assert body["justcall"]["configured"] is True
    assert body["justcall"]["suffix"] == key[-4:]
    saved = env_path.read_text(encoding="utf-8")
    assert f"JUSTCALL_API_KEY={key}" in saved
    assert f"JUSTCALL_API_SECRET={secret}" in saved
    monkeypatch.delenv("JUSTCALL_API_KEY", raising=False)
    monkeypatch.delenv("JUSTCALL_API_SECRET", raising=False)


def test_update_justcall_requires_both_parts(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    monkeypatch.delenv("JUSTCALL_API_KEY", raising=False)
    monkeypatch.delenv("JUSTCALL_API_SECRET", raising=False)
    client = TestClient(app)
    r = client.post("/api/keys", json={"justcall_api_key": "jc_key_abcdefgh"})
    assert r.status_code == 400
    monkeypatch.delenv("JUSTCALL_API_KEY", raising=False)
    monkeypatch.delenv("JUSTCALL_API_SECRET", raising=False)
    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/integrations/justcall/sync")
    assert r.status_code == 503
