"""Self-hosted ASR module. Models are mocked — CI must not download weights."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from backend import transcribe_selfhosted as mod

CL37_TURNS = {380: 57, 362: 48}


@pytest.fixture(autouse=True)
def _reset_models():
    mod.reset_models()
    yield
    mod.reset_models()


def test_merge_result_is_hear_shape():
    turns = [
        (0.0, 1.0, "speaker_1"),
        (1.0, 2.0, "speaker_2"),
        (2.0, 3.0, "speaker_1"),
    ]
    words = [
        {"word": "Hello", "start": 0.0, "end": 0.4},
        {"word": "Katrina", "start": 0.4, "end": 0.9},
        {"word": "Hi", "start": 1.1, "end": 1.4},
        {"word": "there", "start": 1.4, "end": 1.8},
        {"word": "Thanks", "start": 2.1, "end": 2.6},
    ]
    result = mod.merge_result(turns, words, 3.0)
    assert set(result) == {"segments", "words", "speakers", "text", "audio_seconds"}
    assert result["speakers"] == 2
    assert result["audio_seconds"] == 3.0
    assert [s["speaker"] for s in result["segments"]] == [
        "speaker_1",
        "speaker_2",
        "speaker_1",
    ]
    assert "Hello Katrina" in result["segments"][0]["text"]
    assert all("speaker" in w and "word" in w for w in result["words"])
    assert all("channel" in s and "start" in s and "end" in s and "text" in s for s in result["segments"])


def test_gap_word_gets_nearest_turn():
    turns = [(0.0, 1.0, "speaker_1"), (2.0, 3.0, "speaker_2")]
    words = [{"word": "um", "start": 1.4, "end": 1.5}]
    result = mod.merge_result(turns, words, 3.0)
    assert result["words"][0]["speaker"] in {"speaker_1", "speaker_2"}
    assert result["speakers"] == 1


def test_empty_turns_yields_empty_segments():
    result = mod.merge_result([], [{"word": "Hi", "start": 0, "end": 0.2}], 1.0)
    assert result["segments"] == []
    assert result["speakers"] == 0
    assert result["words"] == []


def test_pipeline_loads_once(monkeypatch):
    n = {"c": 0}

    def fake_load():
        n["c"] += 1
        return object()

    monkeypatch.setattr(mod, "_load_pipeline", fake_load)
    assert mod.get_pipeline() is mod.get_pipeline()
    assert n["c"] == 1


def test_whisper_loads_once(monkeypatch):
    n = {"c": 0}

    def fake_load():
        n["c"] += 1
        return object()

    monkeypatch.setattr(mod, "_load_whisper", fake_load)
    assert mod.get_whisper() is mod.get_whisper()
    assert n["c"] == 1


def test_load_pipeline_requires_hf_token(monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HF_TOKEN"):
        mod._load_pipeline()


def test_transcribe_selfhosted_matches_save_transcript_shape(monkeypatch, tmp_path):
    src = tmp_path / "call.mp3"
    src.write_bytes(b"not-real-audio")
    wav = tmp_path / "call.wav"
    wav.write_bytes(b"wav")

    monkeypatch.setattr(mod, "_to_wav16k_mono", lambda _p: str(wav))
    monkeypatch.setattr(
        mod,
        "diarize",
        lambda _p: [
            (0.0, 1.0, "speaker_1"),
            (1.0, 2.2, "speaker_2"),
            (2.2, 3.0, "speaker_1"),
        ],
    )
    monkeypatch.setattr(
        mod,
        "transcribe_words",
        lambda _p: (
            [
                {"word": "Hello", "start": 0.1, "end": 0.4},
                {"word": "there", "start": 1.2, "end": 1.6},
                {"word": "Bye", "start": 2.4, "end": 2.8},
            ],
            3.0,
        ),
    )

    job_id, result, mode = mod.transcribe_selfhosted(str(src), call_id=380)
    assert mode == "selfhosted"
    assert job_id.startswith("selfhosted_")
    assert result["speakers"] == 2
    assert len(result["segments"]) >= 2
    assert result["audio_seconds"] == 3.0
    assert isinstance(result["text"], str) and result["text"]
    # Same keys save_transcript() reads off a Hear payload.
    for key in ("segments", "words", "speakers", "text", "audio_seconds"):
        assert key in result
    assert not wav.exists()


def test_diarize_reads_pyannote4_output(monkeypatch):
    class _Turn:
        def __init__(self, start, end):
            self.start = start
            self.end = end

    class _Ann:
        def itertracks(self, yield_label=True):
            yield _Turn(0.0, 1.0), None, "SPEAKER_00"
            yield _Turn(1.0, 2.0), None, "SPEAKER_01"

    class _Out:
        speaker_diarization = _Ann()

    monkeypatch.setattr(mod, "get_pipeline", lambda: (lambda _file: _Out()))
    monkeypatch.setattr(mod, "_wav_as_file", lambda _p: {"waveform": None, "sample_rate": 16000})
    turns = mod.diarize("/tmp/x.wav")
    assert turns == [(0.0, 1.0, "speaker_1"), (1.0, 2.0, "speaker_2")]


def test_transcribe_words_from_whisper_segments(monkeypatch):
    w1 = SimpleNamespace(word=" Hello ", start=0.0, end=0.3)
    w2 = SimpleNamespace(word="Katrina", start=0.3, end=0.8)
    seg = SimpleNamespace(words=[w1, w2])
    info = SimpleNamespace(duration=12.5)

    class _Model:
        def transcribe(self, path, word_timestamps=True, vad_filter=False):
            assert word_timestamps is True
            return [seg], info

    monkeypatch.setattr(mod, "get_whisper", lambda: _Model())
    words, duration = mod.transcribe_words("/tmp/x.wav")
    assert duration == 12.5
    assert [w["word"] for w in words] == ["Hello", "Katrina"]


def _it_audio(call_id: int) -> str | None:
    root = (os.getenv("CALLPROOF_SELFHOSTED_AUDIO_DIR") or "").strip()
    if not root:
        return None
    for name in (f"{call_id}.wav", f"{call_id}.mp3"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            return path
    return None


@pytest.mark.skipif(
    os.getenv("CALLPROOF_SELFHOSTED_IT") != "1",
    reason="Set CALLPROOF_SELFHOSTED_IT=1 and CALLPROOF_SELFHOSTED_AUDIO_DIR to replay CL-37.",
)
def test_cl37_pyannote_turn_counts():
    """Live models. Expect the 57 / 48 pyannote turns CL-37 measured, not PyAI's 2."""
    for call_id, expected in CL37_TURNS.items():
        path = _it_audio(call_id)
        assert path, f"missing audio for call {call_id}"
        wav = path if path.endswith(".wav") else mod._to_wav16k_mono(path)
        try:
            turns = mod.diarize(wav)
        finally:
            if wav != path and os.path.exists(wav):
                os.remove(wav)
        assert len(turns) == expected, (call_id, len(turns), expected)
        assert {t[2] for t in turns} == {"speaker_1", "speaker_2"}
