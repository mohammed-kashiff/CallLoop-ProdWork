"""Hear must send channel XOR diarize, and detect fake stereo."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import wave

from backend import transcribe as transcribe_mod
from backend.transcribe import (
    _pcm16_true_stereo,
    _separation_fields,
    channel_split_ok,
    expand_tagged_segments,
    has_true_stereo,
    normalize_hear_result,
    resolve_separation_mode,
    speaker_split_ok,
    transcribe_with_fallback,
)


def _wav(path: str, channels: int, pairs: list[tuple[int, int]] | list[int]) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        frames = b""
        if channels == 1:
            for sample in pairs:
                frames += struct.pack("<h", int(sample))
        else:
            for left, right in pairs:
                frames += struct.pack("<hh", int(left), int(right))
        wav.writeframes(frames)


def test_separation_fields_never_send_both():
    channel = _separation_fields("channel")
    diarize = _separation_fields("diarize")
    auto = _separation_fields()
    both = _separation_fields("both")
    assert channel == {"channel": True} and "diarize" not in channel
    assert diarize == {"diarize": True} and "channel" not in diarize
    assert auto == {"channel": True}
    assert both == {"channel": True}


def test_pcm_detects_duplicated_vs_independent_channels():
    fake = b"".join(struct.pack("<hh", 1200, 1200) for _ in range(40))
    real = b"".join(struct.pack("<hh", 1200, 0) for _ in range(40))
    assert _pcm16_true_stereo(fake) is False
    assert _pcm16_true_stereo(real) is True


def test_resolve_mode_on_wav_files(monkeypatch):
    monkeypatch.setenv("HEAR_SEPARATION_MODE", "auto")
    pairs = [(900, 900)] * 80
    split = [(900, 0)] * 80
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "fake.wav")
        true = os.path.join(tmp, "true.wav")
        _wav(fake, 2, pairs)
        _wav(true, 2, split)
        assert has_true_stereo(fake) is False
        assert has_true_stereo(true) is True
        assert resolve_separation_mode(fake) == "diarize"
        assert resolve_separation_mode(true) == "channel"


def test_channel_split_ok_rejects_tagged_dump():
    dump = {
        "speakers": 1,
        "text": "[speaker_1] Hey Dylan. [speaker_2] Oh hey.",
        "segments": [
            {
                "speaker": None,
                "start": 0,
                "end": 90,
                "text": "[speaker_1] Hey Dylan. [speaker_2] Oh hey.",
            }
        ],
    }
    turns = {
        "speakers": 2,
        "segments": [
            {"speaker": "speaker_1", "channel": 0, "text": "Hey", "start": 0, "end": 1},
            {"speaker": "speaker_2", "channel": 1, "text": "Hi", "start": 1, "end": 2},
            {"speaker": "speaker_1", "channel": 0, "text": "Sorry", "start": 2, "end": 3},
            {"speaker": "speaker_2", "channel": 1, "text": "Lead", "start": 3, "end": 4},
        ],
    }
    assert channel_split_ok(dump) is False
    assert channel_split_ok(turns) is True


def test_expand_tagged_blob_splits_speakers():
    rows = expand_tagged_segments(
        [
            {
                "speaker": None,
                "start": 0,
                "end": 10,
                "text": "[speaker_1] Hi Ariel. [speaker_2] Good morning.",
            }
        ]
    )
    assert [r["speaker"] for r in rows] == ["speaker_1", "speaker_2"]
    assert "Hi Ariel" in rows[0]["text"]
    assert "Good morning" in rows[1]["text"]


def test_unlabeled_blob_is_not_split_ok():
    blob = {
        "speakers": 1,
        "text": "Hello. Hey Katrina. I'm calling from InvestorList.",
        "segments": [
            {
                "speaker": None,
                "channel": None,
                "start": 0,
                "end": 234,
                "text": "Hello. Hey Katrina. I'm calling from InvestorList.",
            }
        ],
    }
    assert speaker_split_ok(blob) is False
    assert channel_split_ok(blob) is False


def test_normalize_maps_channel_to_speaker():
    raw = {
        "speakers": 1,
        "text": "Hey. Hi.",
        "segments": [
            {"speaker": None, "channel": 0, "start": 0, "end": 1, "text": "Hey."},
            {"speaker": None, "channel": 1, "start": 1, "end": 2, "text": "Hi."},
        ],
    }
    out = normalize_hear_result(raw)
    assert [s["speaker"] for s in out["segments"]] == ["speaker_1", "speaker_2"]
    assert out["speakers"] == 2
    assert speaker_split_ok(out) is True


def test_normalize_rebuilds_turns_from_words():
    raw = {
        "speakers": 1,
        "text": "Hello Katrina I'm good",
        "segments": [
            {"speaker": None, "start": 0, "end": 4, "text": "Hello Katrina I'm good"}
        ],
        "words": [
            {"word": "Hello", "start": 0, "end": 0.5, "speaker": "speaker_1"},
            {"word": "Katrina", "start": 0.5, "end": 1.0, "speaker": "speaker_1"},
            {"word": "I'm", "start": 1.0, "end": 1.2, "speaker": "speaker_2"},
            {"word": "good", "start": 1.2, "end": 1.5, "speaker": "speaker_2"},
        ],
    }
    out = normalize_hear_result(raw)
    assert [s["speaker"] for s in out["segments"]] == ["speaker_1", "speaker_2"]
    assert "Hello" in out["segments"][0]["text"]
    assert speaker_split_ok(out) is True


def test_fallback_picks_diarize_turns_over_unlabeled(monkeypatch):
    unlabeled = {
        "speakers": 1,
        "text": "Hello Katrina I'm good thanks",
        "segments": [
            {"speaker": None, "start": 0, "end": 10, "text": "Hello Katrina I'm good thanks"}
        ],
        "words": [],
    }
    turns = {
        "speakers": 2,
        "text": "Hello Katrina I'm good thanks",
        "segments": [
            {"speaker": "speaker_1", "start": 0, "end": 2, "text": "Hello Katrina"},
            {"speaker": "speaker_2", "start": 2, "end": 4, "text": "I'm good thanks"},
            {"speaker": "speaker_1", "start": 4, "end": 6, "text": "Awesome"},
        ],
        "words": [],
    }
    jobs = [("job_channel", unlabeled), ("job_diarize", turns)]
    idx = {"i": 0}

    def fake_submit(path, call_id=None, mode=None):
        jid, _payload = jobs[idx["i"]]
        idx["i"] += 1
        return jid

    def fake_poll(job_id):
        for jid, payload in jobs:
            if jid == job_id:
                return json.loads(json.dumps(payload))
        raise AssertionError(job_id)

    monkeypatch.setattr(transcribe_mod, "submit_job_file", fake_submit)
    monkeypatch.setattr(transcribe_mod, "poll_job", fake_poll)
    monkeypatch.setattr(transcribe_mod, "has_true_stereo", lambda p: False)
    monkeypatch.setattr(transcribe_mod, "is_hear_wav", lambda *a, **k: True)
    monkeypatch.setattr(transcribe_mod, "make_hear_copy", lambda *a, **k: None)
    monkeypatch.setenv("HEAR_SEPARATION_MODE", "channel")
    jid, result, mode = transcribe_with_fallback("/tmp/a.wav", "/tmp/a.hear.wav")
    assert mode == "diarize"
    assert jid == "job_diarize"
    assert speaker_split_ok(result)


def test_auto_mixed_skips_channel(monkeypatch):
    turns = {
        "speakers": 2,
        "segments": [
            {"speaker": "speaker_1", "start": 0, "end": 1, "text": "Hi"},
            {"speaker": "speaker_2", "start": 1, "end": 2, "text": "Hello"},
        ],
    }
    modes = []

    def fake_submit(path, call_id=None, mode=None):
        modes.append(mode)
        return "job_d"

    def fake_poll(job_id):
        return dict(turns)

    monkeypatch.setattr(transcribe_mod, "submit_job_file", fake_submit)
    monkeypatch.setattr(transcribe_mod, "poll_job", fake_poll)
    monkeypatch.setattr(transcribe_mod, "has_true_stereo", lambda p: False)
    monkeypatch.setattr(transcribe_mod, "is_hear_wav", lambda *a, **k: True)
    monkeypatch.setenv("HEAR_SEPARATION_MODE", "auto")
    _jid, _result, mode = transcribe_with_fallback("/tmp/mix.wav", "/tmp/mix.hear.wav")
    assert modes == ["diarize"]
    assert mode == "diarize"


def test_auto_true_stereo_uses_channel_only(monkeypatch):
    turns = {
        "speakers": 2,
        "segments": [
            {"speaker": "speaker_1", "channel": 0, "start": 0, "end": 1, "text": "Hi"},
            {"speaker": "speaker_2", "channel": 1, "start": 1, "end": 2, "text": "Hello"},
        ],
    }
    modes = []

    def fake_submit(path, call_id=None, mode=None):
        modes.append(mode)
        return "job_c"

    def fake_poll(job_id):
        return json.loads(json.dumps(turns))

    monkeypatch.setattr(transcribe_mod, "submit_job_file", fake_submit)
    monkeypatch.setattr(transcribe_mod, "poll_job", fake_poll)
    monkeypatch.setattr(transcribe_mod, "has_true_stereo", lambda p: True)
    monkeypatch.setattr(transcribe_mod, "is_hear_wav", lambda *a, **k: True)
    monkeypatch.setenv("HEAR_SEPARATION_MODE", "auto")
    _jid, result, mode = transcribe_with_fallback("/tmp/dual.wav", "/tmp/dual.hear.wav")
    assert modes == ["channel"]
    assert mode == "channel"
    assert speaker_split_ok(result)
