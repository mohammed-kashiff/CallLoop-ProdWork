"""CL-39: transcribe_audio dispatches on the org flag. PyAI path stays the default."""

from __future__ import annotations

from backend.paths import ROOT
from backend.transcribe import transcribe_audio

ORG = "00000000-0000-4000-8000-0000000000aa"
HEAR_SHAPE = {
    "speakers": 2,
    "text": "Hi there",
    "audio_seconds": 2.0,
    "segments": [
        {"speaker": "speaker_1", "start": 0, "end": 1, "text": "Hi"},
        {"speaker": "speaker_2", "start": 1, "end": 2, "text": "there"},
    ],
    "words": [],
}


def test_dispatch_default_uses_pyai(monkeypatch):
    called = {"pyai": 0, "self": 0}

    def fake_pyai(src_path, hear_tmp, call_id=None):
        called["pyai"] += 1
        assert src_path == "/tmp/a.wav"
        assert hear_tmp == "/tmp/a.hear.wav"
        return "job_pyai", dict(HEAR_SHAPE), "diarize"

    def fake_self(src_path, call_id=None):
        called["self"] += 1
        raise AssertionError("self-hosted must not run when the flag is unset")

    monkeypatch.setattr(
        "backend.org_features.features_for_org",
        lambda org_id: {"use_selfhosted_transcription": False},
    )
    monkeypatch.setattr("backend.transcribe.transcribe_with_fallback", fake_pyai)
    monkeypatch.setattr(
        "backend.transcribe_selfhosted.transcribe_selfhosted", fake_self,
    )
    job_id, result, mode = transcribe_audio(
        "/tmp/a.wav", "/tmp/a.hear.wav", call_id="c1", org_id=ORG,
    )
    assert called == {"pyai": 1, "self": 0}
    assert job_id == "job_pyai"
    assert mode == "diarize"
    assert result["speakers"] == 2


def test_dispatch_on_uses_selfhosted(monkeypatch):
    called = {"pyai": 0, "self": 0}

    def fake_pyai(*_a, **_k):
        called["pyai"] += 1
        raise AssertionError("PyAI must not run when the flag is on")

    def fake_self(src_path, call_id=None):
        called["self"] += 1
        assert src_path == "/tmp/a.wav"
        return "selfhosted_abc", dict(HEAR_SHAPE), "selfhosted"

    monkeypatch.setattr(
        "backend.org_features.features_for_org",
        lambda org_id: {
            "show_usage_bar": True,
            "use_selfhosted_transcription": True,
        },
    )
    monkeypatch.setattr("backend.transcribe.transcribe_with_fallback", fake_pyai)
    monkeypatch.setattr(
        "backend.transcribe_selfhosted.transcribe_selfhosted", fake_self,
    )
    job_id, result, mode = transcribe_audio(
        "/tmp/a.wav", "/tmp/a.hear.wav", call_id="c1", org_id=ORG,
    )
    assert called == {"pyai": 0, "self": 1}
    assert job_id == "selfhosted_abc"
    assert mode == "selfhosted"
    assert result["speakers"] == 2


def test_dispatch_off_after_on_returns_to_pyai(monkeypatch):
    state = {"on": True}
    engines = []

    monkeypatch.setattr(
        "backend.org_features.features_for_org",
        lambda org_id: {"use_selfhosted_transcription": state["on"]},
    )
    monkeypatch.setattr(
        "backend.transcribe.transcribe_with_fallback",
        lambda *_a, **_k: ("job_pyai", dict(HEAR_SHAPE), "diarize"),
    )
    monkeypatch.setattr(
        "backend.transcribe_selfhosted.transcribe_selfhosted",
        lambda *_a, **_k: ("job_sh", dict(HEAR_SHAPE), "selfhosted"),
    )
    _jid, _r, mode = transcribe_audio("/tmp/a.wav", "/tmp/a.hear.wav", org_id=ORG)
    engines.append(mode)
    state["on"] = False
    _jid, _r, mode = transcribe_audio("/tmp/a.wav", "/tmp/a.hear.wav", org_id=ORG)
    engines.append(mode)
    assert engines == ["selfhosted", "diarize"]


def test_api_ingest_and_retranscribe_go_through_dispatch():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    assert "transcribe.transcribe_audio(" in src
    assert "transcribe_with_fallback" not in src
    assert src.count("transcribe.transcribe_audio(") == 2
