"""CL-40: HTTP client for the Modal self-hosted service. No real network
calls — httpx is mocked.
"""

from __future__ import annotations

import pytest

from backend import transcribe_selfhosted_remote as mod


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("SELFHOSTED_TRANSCRIBE_URL", "https://mohammed-kashif2911--callloop-transcribe-gpu-fastapi-app.modal.run")
    yield


def test_missing_url_raises(monkeypatch):
    monkeypatch.delenv("SELFHOSTED_TRANSCRIBE_URL", raising=False)
    with pytest.raises(RuntimeError, match="SELFHOSTED_TRANSCRIBE_URL"):
        mod._service_url()


def test_transcribe_remote_sends_request_and_parses_response(monkeypatch, tmp_path):
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"not-real-audio")

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

    def fake_post(url, *, files, timeout, follow_redirects):
        captured["url"] = url
        captured["timeout"] = timeout
        captured["follow_redirects"] = follow_redirects
        captured["filename"] = files["audio"][0]
        return _Resp()

    monkeypatch.setattr(mod.httpx, "post", fake_post)

    job_id, result, mode = mod.transcribe_remote(str(audio), call_id=380)

    assert captured["url"] == "https://mohammed-kashif2911--callloop-transcribe-gpu-fastapi-app.modal.run/transcribe"
    assert captured["follow_redirects"] is True
    assert captured["filename"] == "call.mp3"
    assert captured["timeout"] == mod.REQUEST_TIMEOUT
    assert job_id == "selfhosted_abc123"
    assert mode == "selfhosted"
    assert result["speakers"] == 1


def test_transcribe_remote_raises_on_non_200(monkeypatch, tmp_path):
    audio = tmp_path / "call.mp3"
    audio.write_bytes(b"not-real-audio")

    class _Resp:
        status_code = 503
        text = "service unavailable"

    monkeypatch.setattr(mod.httpx, "post", lambda *a, **k: _Resp())

    with pytest.raises(RuntimeError, match="503"):
        mod.transcribe_remote(str(audio), call_id=380)
