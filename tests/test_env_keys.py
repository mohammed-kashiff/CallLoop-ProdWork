"""Env key format checks. Never stores real secrets in tests."""

from __future__ import annotations

import pytest

from backend import env_keys


def test_normalize_pyai_key_accepts_live_and_sandbox():
    live = env_keys.normalize_pyai_key("pyai_live_" + ("x" * 16))
    sandbox = env_keys.normalize_pyai_key("pyai_test_" + ("x" * 16))
    assert live.startswith("pyai_live_")
    assert sandbox.startswith("pyai_test_")
    assert env_keys.pyai_kind(live) == "live"
    assert env_keys.pyai_kind(sandbox) == "sandbox"


def test_normalize_pyai_key_rejects_junk():
    with pytest.raises(ValueError):
        env_keys.normalize_pyai_key("not-a-key")
    with pytest.raises(ValueError):
        env_keys.normalize_pyai_key("pyai_live_short")


def test_normalize_anthropic_key_rejects_junk():
    with pytest.raises(ValueError):
        env_keys.normalize_anthropic_key("sk-openai-nope")
    ok = env_keys.normalize_anthropic_key("sk-ant-" + ("x" * 16))
    assert ok.startswith("sk-ant-")


def test_normalize_justcall_tokens():
    key = env_keys.normalize_justcall_key("jc_key_abcdefgh")
    secret = env_keys.normalize_justcall_secret("jc_secret_ijklmnop")
    assert key == "jc_key_abcdefgh"
    assert secret == "jc_secret_ijklmnop"


def test_normalize_justcall_rejects_spaces_and_colons():
    with pytest.raises(ValueError):
        env_keys.normalize_justcall_key("abc defghijkl")
    with pytest.raises(ValueError):
        env_keys.normalize_justcall_secret("abc:defghijkl")
    with pytest.raises(ValueError):
        env_keys.normalize_justcall_key("short")
    key = "pyai_live_" + ("abcdefghij" * 2)
    assert env_keys.key_suffix(key) == key[-4:]
    assert env_keys.key_suffix("") is None


def test_env_keys_does_not_write_files():
    assert not hasattr(env_keys, "upsert_env_value")


def test_update_keys_rejects_app_owned_secrets(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    env_path = tmp_path / ".env"
    env_path.write_text("AUDIT_MODE=hybrid\n", encoding="utf-8")
    live = "pyai_live_" + ("k" * 16)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/keys", json={"pyai_api_key": live})
    assert r.status_code == 400, r.text
    assert live not in r.text
    assert "host environment" in r.json()["detail"].lower()
    assert "PYAI_API_KEY=" not in env_path.read_text(encoding="utf-8")


def test_update_keys_rejects_anthropic(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    fake = "sk-ant-" + ("z" * 16)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/keys", json={"anthropic_api_key": fake})
    assert r.status_code == 400
    assert fake not in r.text
