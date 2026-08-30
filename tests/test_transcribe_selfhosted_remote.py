"""CL-40: HTTP client for the Cloud Run self-hosted service. No real network
calls or Google auth — token minting and httpx are both mocked.
"""

from __future__ import annotations

import json

import pytest

from backend import transcribe_selfhosted_remote as mod

SERVICE_JSON = json.dumps({"type": "service_account", "project_id": "calloop-transcript-selfhosted"})


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    mod.reset_token_cache()
    monkeypatch.setenv("SELFHOSTED_TRANSCRIBE_URL", "https://selfhosted-abc.a.run.app")
    monkeypatch.setenv("SELFHOSTED_GCP_SERVICE_ACCOUNT_JSON", SERVICE_JSON)
    yield
    mod.reset_token_cache()


def test_missing_url_raises(monkeypatch):
    monkeypatch.delenv("SELFHOSTED_TRANSCRIBE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SELFHOSTED_TRANSCRIBE_URL"):
        mod._service_url()


def test_missing_service_account_raises(monkeypatch):
    monkeypatch.delenv("SELFHOSTED_GCP_SERVICE_ACCOUNT_JSON", raising=False)
    with pytest.raises(RuntimeError, match="SELFHOSTED_GCP_SERVICE_ACCOUNT_JSON"):
        mod._service_account_info()


def test_token_is_minted_once_and_cached(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(audience):
        calls["n"] += 1
        assert audience == "https://selfhosted-abc.a.run.app"
        return "id-token-1"

    monkeypatch.setattr(mod, "_fetch_id_token", fake_fetch)
    t1 = mod._get_id_token("https://selfhosted-abc.a.run.app")
    t2 = mod._get_id_token("https://selfhosted-abc.a.run.app")
    assert t1 == t2 == "id-token-1"
    assert calls["n"] == 1


def test_transcribe_remote_sends_auth_header_and_parses_response(monkeypatch, tmp_path):
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"not-real-audio")

    monkeypatch.setattr(mod, "_fetch_id_token", lambda audience: "id-token-xyz")

    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {
                "job_id": "selfhosted_abc123",
                "mode": "selfhosted",
                "result": {
                    "segments": [{"speaker": "speaker_1", "start": 0, "end": 1, "text": "hi"}],
                    "words": [],
                    "speakers": 1,
                    "text": "hi",
                    "audio_seconds": 1.0,
                },
            }

    def fake_post(url, *, files, headers, timeout):
        captured["url"] = url
        captured["headers"] = headers
        captured["timeout"] = timeout
        captured["filename"] = files["audio"][0]
        return _Resp()

    monkeypatch.setattr(mod.httpx, "post", fake_post)

    job_id, result, mode = mod.transcribe_remote(str(audio), call_id=380)

    assert captured["url"] == "https://selfhosted-abc.a.run.app/transcribe"
    assert captured["headers"]["Authorization"] == "Bearer id-token-xyz"
    assert captured["filename"] == "call.mp3"
    assert captured["timeout"] == mod.REQUEST_TIMEOUT
    assert job_id == "selfhosted_abc123"
    assert mode == "selfhosted"
    assert result["speakers"] == 1


def test_transcribe_remote_raises_on_non_200(monkeypatch, tmp_path):
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"not-real-audio")
    monkeypatch.setattr(mod, "_fetch_id_token", lambda audience: "id-token-xyz")

    class _Resp:
        status_code = 503
        text = "service unavailable"

    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match="503"):
        mod.transcribe_remote(str(audio), call_id=380)
