"""Hear must send channel XOR diarize, and detect fake stereo."""

from __future__ import annotations

import os
import struct
import tempfile
import wave

from backend.transcribe import (
    _pcm16_true_stereo,
    _separation_fields,
    expand_tagged_segments,
    has_true_stereo,
    resolve_separation_mode,
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
    assert auto == {"diarize": True}
    assert both == {"diarize": True}


def test_pcm_detects_duplicated_vs_independent_channels():
    fake = b"".join(struct.pack("<hh", 1200, 1200) for _ in range(40))
    real = b"".join(struct.pack("<hh", 1200, 0) for _ in range(40))
    assert _pcm16_true_stereo(fake) is False
    assert _pcm16_true_stereo(real) is True


def test_resolve_mode_on_wav_files():
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
