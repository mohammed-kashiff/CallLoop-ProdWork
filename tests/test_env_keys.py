"""Env key upsert and validation. Never stores real secrets in tests."""

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


def test_upsert_overwrites_and_does_not_leak_path_value(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PYAI_API_KEY=pyai_test_oldvaluexxxxxxxxx\nAUDIT_MODE=hybrid\n", encoding="utf-8")
    new = "pyai_live_" + ("y" * 16)
    outcome = env_keys.upsert_env_value(str(path), "PYAI_API_KEY", new, overwrite=True)
    assert outcome == "written"
    text = path.read_text(encoding="utf-8")
    assert new in text
    assert "pyai_test_oldvaluexxxxxxxxx" not in text
    assert "AUDIT_MODE=hybrid" in text


def test_upsert_keeps_existing_when_overwrite_false(tmp_path):
    path = tmp_path / ".env"
    path.write_text("PYAI_API_KEY=pyai_live_keepmepleasexxxx\n", encoding="utf-8")
    env_keys.upsert_env_value(
        str(path), "PYAI_API_KEY", "pyai_test_" + ("z" * 16), overwrite=False,
    )
    assert "pyai_live_keepmepleasexxxx" in path.read_text(encoding="utf-8")


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


def test_update_keys_persists_and_does_not_echo_secret(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    env_path = tmp_path / ".env"
    env_path.write_text("AUDIT_MODE=hybrid\n", encoding="utf-8")
    monkeypatch.setattr("backend.api.ENV_FILE", str(env_path))
    live = "pyai_live_" + ("k" * 16)
    client = TestClient(app)
    r = client.post("/api/keys", json={"pyai_api_key": live})
    assert r.status_code == 200, r.text
    body = r.json()
    assert live not in r.text
    assert body["ok"] is True
    assert body["pyai"]["label"] == "Live"
    assert body["pyai"]["suffix"] == live[-4:]
    saved = env_path.read_text(encoding="utf-8")
    assert f"PYAI_API_KEY={live}" in saved
