"""Self-hosted ASR + diarization. Sibling to backend/transcribe.py (PyAI Hear).

Not wired into live routes (CL-39). Returns the same result shape Hear already
produces so save_transcript / replace_transcript / the segments table / the
frontend never see which engine ran:

    {segments, words, speakers, text, audio_seconds}

Models load once per process (lazy, locked). Requires HF_TOKEN on the host.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import wave as wave_mod
from array import array

from . import applog
from .config import load_env

load_env()
applog.setup_logging()

log = logging.getLogger("callproof.transcribe_selfhosted")

DIARIZE_CHECKPOINT = "pyannote/speaker-diarization-3.1"
WHISPER_MODEL = (os.getenv("SELFHOSTED_WHISPER_MODEL") or "small").strip() or "small"
TARGET_SAMPLE_RATE = 16000
FFMPEG_TIMEOUT = 60

_pipeline = None
_whisper = None
_lock = threading.Lock()


def _hf_token() -> str:
    return (
        os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN") or ""
    ).strip()


def _ffmpeg_bin() -> str | None:
    env = (os.getenv("FFMPEG_PATH") or "").strip()
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None
    if exe and os.path.isfile(exe) and os.access(exe, os.X_OK):
        return exe
    return None


def reset_models() -> None:
    """Drop cached models. Tests only."""
    global _pipeline, _whisper
    with _lock:
        _pipeline = None
        _whisper = None


def _load_pipeline():
    token = _hf_token()
    if not token:
        raise RuntimeError(
            "HF_TOKEN is not set. Set it on the host environment "
            "(gated pyannote/speaker-diarization-3.1)."
        )
    from pyannote.audio import Pipeline

    return Pipeline.from_pretrained(DIARIZE_CHECKPOINT, token=token)


def _load_whisper():
    from faster_whisper import WhisperModel

    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


def get_pipeline():
    global _pipeline
    with _lock:
        if _pipeline is None:
            t0 = time.perf_counter()
            _pipeline = _load_pipeline()
            applog.event(
                log, "selfhosted_model_loaded",
                kind="pyannote", ms=int((time.perf_counter() - t0) * 1000),
            )
        return _pipeline


def get_whisper():
    global _whisper
    with _lock:
        if _whisper is None:
            t0 = time.perf_counter()
            _whisper = _load_whisper()
            applog.event(
                log, "selfhosted_model_loaded",
                kind="whisper", model=WHISPER_MODEL,
                ms=int((time.perf_counter() - t0) * 1000),
            )
        return _whisper


def _time_key(row: dict) -> tuple[float, float]:
    try:
        start = float(row.get("start") or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(row.get("end") or start)
    except (TypeError, ValueError):
        end = start
    return (start, end)


def _segments_from_words(words: list) -> list:
    """Group consecutive same-speaker words into turns (Hear word-fallback)."""
    prepared: list = []
    for raw in words or []:
        if not isinstance(raw, dict):
            continue
        speaker = raw.get("speaker")
        if speaker in (None, "") or str(speaker).strip() == "":
            continue
        token = str(raw.get("word") or raw.get("text") or "").strip()
        if not token:
            continue
        start, end = _time_key(raw)
        prepared.append(
            {
                "speaker": str(speaker).strip().lower(),
                "channel": raw.get("channel"),
                "start": start,
                "end": end,
                "text": token,
            }
        )
    prepared.sort(key=_time_key)

    turns: list = []
    current = None
    for row in prepared:
        speaker = row["speaker"]
        token = row["text"]
        start = row["start"]
        end = row["end"]
        if current and current["speaker"] == speaker:
            if token[:1] in ",.?!:;":
                current["text"] += token
            else:
                current["text"] += " " + token
            current["end"] = end
            continue
        if current:
            turns.append(current)
        current = {
            "speaker": speaker,
            "channel": row.get("channel"),
            "start": start,
            "end": end,
            "text": token,
        }
    if current:
        turns.append(current)
    return turns


def _hear_speaker_map(labels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for lab in labels:
        if lab not in out:
            out[lab] = f"speaker_{len(out) + 1}"
    return out


def _assign_speaker(mid: float, turns: list[tuple[float, float, str]]) -> str | None:
    for start, end, spk in turns:
        if start <= mid <= end:
            return spk
    if not turns:
        return None
    best = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))
    return best[2]


def _to_wav16k_mono(src_path: str) -> str:
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg is not available. Install ffmpeg or pip install imageio-ffmpeg."
        )
    fd, dest = tempfile.mkstemp(prefix="callproof_selfhosted_", suffix=".wav")
    os.close(fd)
    proc = subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-i", src_path,
            "-map", "0:a:0",
            "-ac", "1",
            "-ar", str(TARGET_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-f", "wav",
            dest,
        ],
        capture_output=True,
        text=True,
        timeout=FFMPEG_TIMEOUT,
        check=False,
    )
    if proc.returncode != 0 or not os.path.isfile(dest) or os.path.getsize(dest) < 1:
        if os.path.exists(dest):
            os.remove(dest)
        err = (proc.stderr or proc.stdout or "ffmpeg failed").strip()
        raise RuntimeError(f"Could not convert audio for self-hosted ASR: {err[:200]}")
    return dest


def _wav_as_file(wav_path: str) -> dict:
    """In-memory waveform so pyannote 4 does not need system libav / torchcodec."""
    import torch

    with wave_mod.open(wav_path, "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
        sw = w.getsampwidth()
    if sw != 2:
        raise RuntimeError(f"expected 16-bit pcm, got sampwidth={sw}")
    samples = array("h")
    samples.frombytes(raw)
    tensor = torch.tensor(samples, dtype=torch.float32)
    if ch > 1:
        tensor = tensor.reshape(-1, ch).mean(dim=1)
    tensor = (tensor / 32768.0).unsqueeze(0)
    return {"waveform": tensor, "sample_rate": sr}


def _annotation_tracks(prediction):
    annotation = getattr(prediction, "speaker_diarization", prediction)
    return annotation.itertracks(yield_label=True)


def diarize(wav_path: str) -> list[tuple[float, float, str]]:
    """Return Hear-labelled turns (speaker_1, …) from pyannote."""
    prediction = get_pipeline()(_wav_as_file(wav_path))
    raw: list[tuple[float, float, str]] = []
    for turn, _, speaker in _annotation_tracks(prediction):
        raw.append((float(turn.start), float(turn.end), str(speaker)))
    raw.sort(key=lambda r: (r[0], r[1]))
    mapping = _hear_speaker_map([r[2] for r in raw])
    return [(a, b, mapping[s]) for a, b, s in raw]


def transcribe_words(wav_path: str) -> tuple[list[dict], float]:
    """faster-whisper words with timestamps. Duration in seconds."""
    segments, info = get_whisper().transcribe(
        wav_path,
        word_timestamps=True,
        vad_filter=False,
    )
    words: list[dict] = []
    for seg in segments:
        for w in seg.words or []:
            token = str(w.word or "").strip()
            if not token:
                continue
            words.append(
                {
                    "word": token,
                    "start": float(w.start),
                    "end": float(w.end),
                }
            )
    duration = float(getattr(info, "duration", 0) or 0)
    if duration <= 0 and words:
        duration = float(words[-1]["end"])
    return words, duration


def merge_result(
    turns: list[tuple[float, float, str]],
    words: list[dict],
    duration: float,
) -> dict:
    labeled: list[dict] = []
    for w in words:
        mid = (float(w["start"]) + float(w["end"])) / 2.0
        spk = _assign_speaker(mid, turns)
        if not spk:
            continue
        labeled.append(
            {
                "word": w["word"],
                "start": float(w["start"]),
                "end": float(w["end"]),
                "speaker": spk,
                "channel": None,
            }
        )
    segments = _segments_from_words(labeled)
    speaker_ids = {
        str(s.get("speaker") or "").strip().lower()
        for s in segments
        if s.get("speaker") not in (None, "")
    }
    text = " ".join(str(s.get("text") or "") for s in segments).strip()
    return {
        "segments": segments,
        "words": labeled,
        "speakers": len(speaker_ids),
        "text": text,
        "audio_seconds": float(duration or 0),
    }


def transcribe_selfhosted(src_path: str, call_id=None) -> tuple[str, dict, str]:
    """Equivalent return to transcribe_with_fallback: (job_id, result, mode)."""
    wav = _to_wav16k_mono(src_path)
    t0 = time.perf_counter()
    try:
        turns = diarize(wav)
        words, duration = transcribe_words(wav)
        result = merge_result(turns, words, duration)
    finally:
        if os.path.exists(wav):
            os.remove(wav)
    job_id = f"selfhosted_{uuid.uuid4().hex[:20]}"
    applog.event(
        log, "selfhosted_transcribe",
        call_id=call_id if call_id is not None else "-",
        pyannote_turns=len(turns),
        segments=len(result.get("segments") or []),
        speakers=result.get("speakers"),
        words=len(result.get("words") or []),
        ms=int((time.perf_counter() - t0) * 1000),
    )
    return job_id, result, "selfhosted"
