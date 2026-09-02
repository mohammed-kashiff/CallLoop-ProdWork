"""
CallProof - transcript spine (with logging).

Submit a local audio file (or public URL) to PyAI Hear, poll until done, and
save a speaker-labelled, timestamped transcript to Postgres. Each source is
transcribed once (cached by content hash). On a failed job, PyAI's actual error
is logged and raised - no more silent failures.
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
import hashlib
import uuid
import shutil
import subprocess
import re
import array
import wave

import httpx
from . import applog
from . import db
from . import pyai_usage
from .config import load_env
from .org_ids import DEFAULT_ORG_ID, org_scope

load_env()
applog.setup_logging()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.transcribe")

PYAI_API_KEY = (os.getenv("PYAI_API_KEY") or "").strip() or None
BASE_URL = "https://api.pyai.com"
RECAP_PACK_ID = os.getenv("RECAP_PACK_ID") or None

# Never send channel+diarize together (Hear forbids it and returns one dump).
# auto: true L/R telephony → channel; mixed/mono MP3 → diarize (not fake stereo).
SEPARATION_MODE = "auto"    # "channel" | "diarize" | "auto"
MODEL = "pyai-hear-telephony"
_STEREO_DIFF_RATIO = 0.08      # L vs R energy; duplicated mono-as-stereo is ~0

# Poll PyAI async Hear jobs. Large/slow batches (and Hear backpressure) need
# more than two minutes — we upload 8 kHz stereo copies, but STT still tracks
# wall time + queue depth, not just upload size.
POLL_INTERVAL_SECONDS = max(1, int(os.getenv("HEAR_POLL_INTERVAL_SECONDS", "3")))
POLL_MAX_ATTEMPTS = max(1, int(os.getenv("HEAR_POLL_MAX_ATTEMPTS", "200")))  # default ~10 min

# Telephony-sized copy for PyAI Hear only. Playback still uses the original file.
# Must stay discrete-channel PCM (not MP3). Joint-stereo MP3 mixes L/R, and
# Hear channel=true then returns no speaker labels.
HEAR_SAMPLE_RATE = 8000
HEAR_CHANNELS = 2
HEAR_FFMPEG_TIMEOUT = 60

# Populated at import; may be refreshed by set_api_key() after sandbox mint.
HEADERS = {"Authorization": f"Bearer {PYAI_API_KEY}"} if PYAI_API_KEY else {}


def set_api_key(api_key: str):
    """Inject / rotate the PyAI key at runtime (used by api.py sandbox mint)."""
    global PYAI_API_KEY, HEADERS
    key = (api_key or "").strip()
    if not key:
        raise ValueError("PYAI_API_KEY cannot be empty")
    PYAI_API_KEY = key
    HEADERS = {"Authorization": f"Bearer {key}"}
    os.environ["PYAI_API_KEY"] = key


def _require_api_key():
    if not PYAI_API_KEY:
        raise RuntimeError(
            "PYAI_API_KEY not configured. Set it on the host environment "
            "(live key with transcribe:jobs for CallProof)."
        )


def is_url(src):
    return src.startswith("http://") or src.startswith("https://")


def _ffmpeg_bin():
    """System ffmpeg, FFMPEG_PATH, or imageio-ffmpeg's bundled binary."""
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


def _run_ffmpeg(ffmpeg, src_path, dest_path, channels=HEAR_CHANNELS):
    # Discrete PCM only. Joint-stereo MP3/AAC bleeds L/R and Hear drops speakers.
    # channel mode: 2 ch; diarize mode: 1 ch (never fake-stereo a mixed recording).
    nch = 2 if int(channels) >= 2 else 1
    return subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel", "error",
            "-nostdin",
            "-y",
            "-i", src_path,
            "-map", "0:a:0",
            "-ac", str(nch),
            "-ar", str(HEAR_SAMPLE_RATE),
            "-c:a", "pcm_s16le",
            "-f", "wav",
            dest_path,
        ],
        capture_output=True,
        text=True,
        timeout=HEAR_FFMPEG_TIMEOUT,
        check=False,
    )


def _pcm16_true_stereo(pcm: bytes) -> bool:
    """True when interleaved s16le L/R are not the same mix duplicated."""
    n = (len(pcm) // 4) * 2  # whole stereo frames → sample count
    if n < 16:
        return False
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    diff = energy = 0
    for i in range(0, len(samples) - 1, 2):
        left, right = samples[i], samples[i + 1]
        diff += abs(left - right)
        energy += abs(left) + abs(right)
    if energy == 0:
        return False
    return (diff / energy) >= _STEREO_DIFF_RATIO


def has_true_stereo(path: str) -> bool:
    """True for independent L/R (telephony dual-channel). False for mono/mixed."""
    if not path or not os.path.isfile(path):
        return False
    try:
        with wave.open(path, "rb") as wav:
            if wav.getnchannels() != 2 or wav.getsampwidth() != 2:
                return False
            n = min(wav.getnframes(), wav.getframerate() * 15)
            return _pcm16_true_stereo(wav.readframes(n))
    except (OSError, wave.Error, EOFError):
        pass
    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        return False
    try:
        proc = subprocess.run(
            [
                ffmpeg,
                "-hide_banner", "-loglevel", "error", "-nostdin",
                "-t", "15", "-i", path,
                "-ac", "2", "-ar", str(HEAR_SAMPLE_RATE),
                "-f", "s16le", "-c:a", "pcm_s16le", "pipe:1",
            ],
            capture_output=True,
            timeout=HEAR_FFMPEG_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0 or not proc.stdout:
        return False
    return _pcm16_true_stereo(proc.stdout)


def resolve_separation_mode(path: str | None = None) -> str:
    """channel XOR diarize. Hear rejects combining them. Mixed files use diarize."""
    raw = (os.getenv("HEAR_SEPARATION_MODE") or SEPARATION_MODE or "auto").strip().lower()
    if raw == "diarize":
        return "diarize"
    if raw == "auto":
        if path and has_true_stereo(path):
            return "channel"
        if path:
            return "diarize"
        return "channel"
    return "channel"


def is_hear_wav(path, channels=None):
    """True if path is already 8 kHz PCM WAV with the requested channel count."""
    want = HEAR_CHANNELS if channels is None else int(channels)
    try:
        with open(path, "rb") as f:
            head = f.read(44)
    except OSError:
        return False
    if len(head) < 44 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
        return False
    fmt_at = head.find(b"fmt ")
    if fmt_at < 0 or fmt_at + 16 > len(head):
        return False
    audio_format = int.from_bytes(head[fmt_at + 8:fmt_at + 10], "little")
    nch = int.from_bytes(head[fmt_at + 10:fmt_at + 12], "little")
    rate = int.from_bytes(head[fmt_at + 12:fmt_at + 16], "little")
    return audio_format == 1 and nch == want and rate == HEAR_SAMPLE_RATE


def make_hear_copy(src_path, dest_path, channels=HEAR_CHANNELS, keep_if_larger=False):
    """
    Encode a smaller 8 kHz stereo PCM WAV for PyAI Hear (channel mode).

    Returns dest_path if the copy exists and is strictly smaller than src.
    Returns None if ffmpeg is missing, transcode fails, or there is no size
    win (typical for recordings that are already 8 kHz stereo WAV). Never
    modifies src_path — playback should keep the original.
    """
    if not src_path or not os.path.isfile(src_path):
        return None
    original_bytes = os.path.getsize(src_path)
    if original_bytes <= 0:
        return None

    ffmpeg = _ffmpeg_bin()
    if not ffmpeg:
        applog.event(
            log, "transcode_skipped",
            reason="ffmpeg_missing",
            original_bytes=original_bytes,
        )
        log.info(
            "Hear transcode skipped (no ffmpeg). Install ffmpeg or "
            "pip install imageio-ffmpeg. Uploading the original file."
        )
        return None

    dest_dir = os.path.dirname(dest_path) or "."
    os.makedirs(dest_dir, exist_ok=True)
    if os.path.exists(dest_path):
        os.remove(dest_path)

    nch = 2 if int(channels) >= 2 else 1
    started = time.perf_counter()
    last_err = None
    try:
        proc = _run_ffmpeg(ffmpeg, src_path, dest_path, channels=nch)
        if proc.returncode == 0 and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            last_err = None
        else:
            err = (proc.stderr or proc.stdout or "").strip().replace("\n", " ")[:200]
            last_err = err or f"ffmpeg exit {proc.returncode}"
            if os.path.exists(dest_path):
                os.remove(dest_path)
    except (OSError, subprocess.TimeoutExpired) as e:
        last_err = f"{type(e).__name__}: {e}"
        log.warning("Hear transcode failed: %s", last_err)
        if os.path.exists(dest_path):
            os.remove(dest_path)

    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    if last_err or not os.path.isfile(dest_path):
        applog.event(
            log, "transcode_skipped",
            reason="ffmpeg_failed",
            original_bytes=original_bytes,
            duration_ms=duration_ms,
            error=last_err or "no_output",
        )
        log.warning(
            "Hear transcode failed (%s); uploading the original file",
            last_err or "no_output",
        )
        return None

    hear_bytes = os.path.getsize(dest_path)
    if hear_bytes >= original_bytes and not keep_if_larger:
        os.remove(dest_path)
        applog.event(
            log, "transcode_skipped",
            reason="no_savings",
            original_bytes=original_bytes,
            hear_bytes=hear_bytes,
            duration_ms=duration_ms,
        )
        log.info(
            "Hear copy not smaller (%d -> %d bytes); uploading original",
            original_bytes, hear_bytes,
        )
        return None

    applog.event(
        log, "transcode_success",
        original_bytes=original_bytes,
        hear_bytes=hear_bytes,
        duration_ms=duration_ms,
        saved_bytes=original_bytes - hear_bytes,
        channels=nch,
    )
    log.info(
        "Hear copy %d -> %d bytes in %.0f ms (%d ch 8 kHz PCM)",
        original_bytes,
        hear_bytes,
        duration_ms,
        nch,
    )
    return dest_path


def prepare_hear_upload(src_path, hear_tmp):
    """Pick channel vs diarize and the file Hear should actually receive."""
    mode = resolve_separation_mode(src_path)
    nch = 2 if mode == "channel" else 1
    if is_hear_wav(src_path, channels=nch):
        return src_path, mode
    copy = make_hear_copy(
        src_path, hear_tmp, channels=nch, keep_if_larger=(mode == "channel"),
    )
    return (copy or src_path), mode


# ---------- Database ----------
def init_db():
    """Confirm Postgres is reachable. Re-seed the placeholder org if wiped."""
    from .auth import ensure_placeholder_org

    with db.connection() as conn:
        ensure_placeholder_org(conn)
        conn.execute("SELECT 1")


def sanitize_filename(name: str | None, fallback: str = "recording.mp3") -> str:
    """Keep a safe display name from an upload (basename only, no path tricks)."""
    raw = (name or "").strip() or fallback
    base = os.path.basename(raw.replace("\\", "/"))
    base = base.strip().lstrip(".")
    if not base:
        base = fallback
    # Cap length; keep extension if present
    if len(base) > 180:
        root, ext = os.path.splitext(base)
        base = root[: 180 - len(ext)] + ext
    return base


def new_pyai_call_id():
    return f"callproof_{uuid.uuid4().hex[:12]}"


def find_existing_call(conn, identity, *, org_id: str):
    return conn.execute(
        """
        SELECT id, pyai_call_id FROM calls
        WHERE org_id = %s AND audio_url = %s AND status = 'completed'
        """,
        (org_id, identity),
    ).fetchone()


def find_existing_external(
    conn, source: str, external_id: str, *, org_id: str,
):
    return conn.execute(
        """
        SELECT id, pyai_call_id FROM calls
        WHERE org_id = %s AND source = %s AND external_id = %s AND status = 'completed'
        """,
        (org_id, source, str(external_id)),
    ).fetchone()


def get_call(conn, call_id: int, *, org_id: str):
    """Return the call row if it belongs to this org, else None."""
    return conn.execute(
        """
        SELECT id, filename, status, audio_url, pyai_call_id
        FROM calls
        WHERE id = %s AND org_id = %s
        """,
        (call_id, org_id),
    ).fetchone()


_SPEAKER_TAG = re.compile(r"\[(speaker_\d+)\]", re.I)
_TOKEN_RE = re.compile(r"[A-Za-z0-9']+")
_MIN_PIECE_SEC = 0.35


def _channel_to_speaker(channel) -> str | None:
    """Hear channel 0 → speaker_1, channel 1 → speaker_2."""
    if channel is None or channel == "":
        return None
    try:
        ch = int(channel)
    except (TypeError, ValueError):
        return None
    if ch < 0:
        return None
    return f"speaker_{ch + 1}"


def _fill_speaker_from_channel(seg: dict) -> dict:
    row = dict(seg)
    if row.get("speaker") in (None, ""):
        label = _channel_to_speaker(row.get("channel"))
        if label:
            row["speaker"] = label
    return row


def _labeled_speaker_ids(segments: list) -> set:
    out = set()
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        sp = seg.get("speaker")
        if sp not in (None, "") and str(sp).strip() != "":
            out.add(str(sp).strip().lower())
    return out


def _time_key(row: dict) -> tuple[float, float]:
    """Sort key for elapsed time. Invalid timestamps sort as 0."""
    try:
        start = float(row.get("start") or 0)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = float(row.get("end") or start)
    except (TypeError, ValueError):
        end = start
    return (start, end)


def _norm_token(raw: str) -> str:
    return re.sub(r"[^a-z0-9']+", "", (raw or "").lower())


def _speech_tokens(text: str) -> list[str]:
    """Word-like tokens used as a speaking-time proxy. Keep in sync with speakerText.ts."""
    return [tok for m in _TOKEN_RE.finditer(text or "") if (tok := _norm_token(m.group(0)))]


def _speech_weight(text: str) -> int:
    return max(len(_speech_tokens(text)), 1)


def words_from_raw_json(raw) -> list | None:
    """Pull Hear word timings from calls.raw_json without assuming a JSON column type."""
    if raw is None:
        return None
    if isinstance(raw, (bytes, memoryview)):
        raw = bytes(raw).decode("utf-8")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None
    if not isinstance(raw, dict):
        return None
    words = raw.get("words")
    return words if isinstance(words, list) else None


def _word_speaker(row: dict) -> str:
    sp = row.get("speaker")
    if sp not in (None, "") and str(sp).strip():
        return str(sp).strip().lower()
    label = _channel_to_speaker(row.get("channel"))
    return (label or "").lower()


def _timed_words(words: list | None) -> list[tuple[float, float, str, str]]:
    rows: list[tuple[float, float, str, str]] = []
    for raw in words or []:
        if not isinstance(raw, dict):
            continue
        token = _norm_token(str(raw.get("word") or raw.get("text") or ""))
        if not token:
            continue
        start, end = _time_key(raw)
        rows.append((start, end, token, _word_speaker(raw)))
    rows.sort(key=lambda r: (r[0], r[1]))
    return rows


def _words_in_span(
    timed: list[tuple[float, float, str, str]], t0: float, t1: float, pad: float = 0.05,
) -> list[tuple[float, float, str, str]]:
    lo, hi = t0 - pad, t1 + pad
    return [w for w in timed if w[1] >= lo and w[0] <= hi]


def _consume_tokens(
    tokens: list[str],
    timed: list[tuple[float, float, str, str]],
    start_at: int,
    speaker: str = "",
) -> tuple[tuple[float, float] | None, int]:
    """Match tokens to consecutive same-speaker words. Skip other speakers in between."""
    if not tokens or not timed:
        return None, start_at
    want = speaker.strip().lower()
    n = len(timed)
    start_i = None
    for j in range(start_at, n):
        if want and timed[j][3] and timed[j][3] != want:
            continue
        if timed[j][2] == tokens[0]:
            start_i = j
            break
    if start_i is None:
        return None, start_at
    k = start_i
    for tok in tokens:
        while k < n and want and timed[k][3] and timed[k][3] != want:
            k += 1
        if k >= n:
            return None, start_at
        if timed[k][2] == tok:
            k += 1
            continue
        nxt = k + 1
        while nxt < n and want and timed[nxt][3] and timed[nxt][3] != want:
            nxt += 1
        if nxt < n and timed[nxt][2] == tok:
            k = nxt + 1
            continue
        return None, start_at
    return (timed[start_i][0], timed[k - 1][1]), k


def _piece_spans(t0: float, t1: float, weights: list[int]) -> list[tuple[float, float]]:
    """Distribute a parent span across pieces by speech weight, not character count."""
    span = max(0.0, t1 - t0)
    if not weights:
        return []
    total = sum(weights) or 1
    durs = [span * (w / total) for w in weights]
    if span >= _MIN_PIECE_SEC * len(weights):
        durs = [max(_MIN_PIECE_SEC, d) for d in durs]
        scale = sum(durs) or 1
        durs = [span * (d / scale) for d in durs]
    out: list[tuple[float, float]] = []
    cursor = t0
    for dur in durs:
        out.append((cursor, cursor + dur))
        cursor += dur
    if out:
        out[-1] = (out[-1][0], t1)
    return out


def _split_tagged_pieces(text: str) -> list[tuple[str, str]]:
    marks = list(_SPEAKER_TAG.finditer(text))
    pieces: list[tuple[str, str]] = []
    for i, m in enumerate(marks):
        body = text[m.end() : (marks[i + 1].start() if i + 1 < len(marks) else len(text))]
        body = body.strip()
        if body:
            pieces.append((m.group(1).lower(), body))
    return pieces


def _stamp_unstamped(rows: list, timed: list[tuple[float, float, str, str]]) -> int:
    """Re-time rows that still have interpolated clocks by walking Hear word timings."""
    if not timed:
        return 0
    cursor = 0
    stamped = 0
    for row in rows:
        if row.get("_stamped"):
            span, nxt = _consume_tokens(
                _speech_tokens(str(row.get("text") or "")),
                timed,
                cursor,
                str(row.get("speaker") or ""),
            )
            if span:
                cursor = nxt
            continue
        span, nxt = _consume_tokens(
            _speech_tokens(str(row.get("text") or "")),
            timed,
            cursor,
            str(row.get("speaker") or ""),
        )
        if not span:
            continue
        old_start, old_end = _time_key(row)
        row["start"], row["end"] = span
        row["_stamped"] = True
        cursor = nxt
        if abs(old_start - span[0]) > 0.05 or abs(old_end - span[1]) > 0.05:
            stamped += 1
    return stamped


def _clamp_tag_splits(rows: list) -> int:
    """Keep interpolated tag-split starts out of a previous turn's real window."""
    clamped = 0
    prev_end: float | None = None
    for row in rows:
        start, end = _time_key(row)
        if (
            row.get("_from_tag")
            and not row.get("_stamped")
            and prev_end is not None
            and start < prev_end
        ):
            start = prev_end
            if end < start:
                end = start
            row["start"] = start
            row["end"] = end
            clamped += 1
        prev_end = _time_key(row)[1]
    return clamped


def _segments_from_words(words: list) -> list:
    """Group consecutive same-speaker words into turns when Hear omitted segment labels."""
    prepared: list = []
    for raw in words or []:
        if not isinstance(raw, dict):
            continue
        row = _fill_speaker_from_channel(raw)
        speaker = row.get("speaker")
        if speaker in (None, "") or str(speaker).strip() == "":
            continue
        token = str(row.get("word") or row.get("text") or "").strip()
        if not token:
            continue
        start, end = _time_key(row)
        prepared.append(
            {
                "speaker": str(speaker).strip().lower(),
                "channel": row.get("channel"),
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


def expand_tagged_segments(segments: list, words: list | None = None) -> list:
    """Split Hear blobs like '[speaker_1] hi [speaker_2] hello' into one row per speaker.

    Character-length interpolation is a poor speech-duration proxy and made
    click-to-seek land inside a neighboring turn. Prefer Hear word timestamps;
    otherwise split the parent span by word count. Keep the fallback in sync
    with frontend/src/lib/speakerText.ts.
    """
    timed = _timed_words(words)
    out: list = []
    tag_pieces = 0
    word_aligned = 0
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        seg = _fill_speaker_from_channel(seg)
        text = str(seg.get("text") or "")
        marks = list(_SPEAKER_TAG.finditer(text))
        if not marks:
            out.append(dict(seg))
            continue
        pieces = _split_tagged_pieces(text)
        if not pieces:
            row = dict(seg)
            row["text"] = _SPEAKER_TAG.sub("", text).strip()
            out.append(row)
            continue
        t0, t1 = _time_key(seg)
        spans = _piece_spans(t0, t1, [_speech_weight(body) for _, body in pieces])
        aligned = None
        local = _words_in_span(timed, t0, t1) if timed else []
        if local:
            cursor = 0
            found: list[tuple[float, float] | None] = []
            for speaker, body in pieces:
                span, nxt = _consume_tokens(_speech_tokens(body), local, cursor, speaker)
                found.append(span)
                if span:
                    cursor = nxt
            if found and all(span is not None for span in found):
                aligned = found
        for i, (speaker, body) in enumerate(pieces):
            row = dict(seg)
            row["speaker"] = speaker
            row["text"] = body
            row["_from_tag"] = True
            tag_pieces += 1
            if aligned:
                row["start"], row["end"] = aligned[i]
                row["_stamped"] = True
                word_aligned += 1
            else:
                row["start"], row["end"] = spans[i]
            out.append(row)
    stamped = _stamp_unstamped(out, timed)
    out.sort(key=_time_key)
    clamped = _clamp_tag_splits(out)
    if tag_pieces or clamped:
        applog.event(
            log, "tagged_segment_times_fixed",
            pieces=tag_pieces,
            word_aligned=word_aligned,
            stamped=stamped,
            clamped=clamped,
        )
    for i, row in enumerate(out):
        row["seq"] = i
        row.pop("_from_tag", None)
        row.pop("_stamped", None)
    return out


def normalize_hear_result(result: dict) -> dict:
    """Fill speaker from channel, rebuild turns from words, split [speaker_N] blobs."""
    if not isinstance(result, dict):
        return result
    out = dict(result)
    segs = [
        _fill_speaker_from_channel(s)
        for s in (out.get("segments") or [])
        if isinstance(s, dict)
    ]
    words = [
        _fill_speaker_from_channel(w)
        for w in (out.get("words") or [])
        if isinstance(w, dict)
    ]
    if words:
        out["words"] = words
    if len(_labeled_speaker_ids(segs)) < 2:
        rebuilt = _segments_from_words(words)
        if len(_labeled_speaker_ids(rebuilt)) >= 2:
            applog.event(
                log, "hear_segments_from_words",
                turns=len(rebuilt),
                speakers=len(_labeled_speaker_ids(rebuilt)),
            )
            segs = rebuilt
    out["segments"] = expand_tagged_segments(segs, words=words)
    speakers = _labeled_speaker_ids(out["segments"])
    if speakers:
        out["speakers"] = len(speakers)
    return out


def save_transcript(
    conn, identity, job_id, result, pyai_call_id=None, filename=None,
    source=None, external_id=None, *, org_id: str,
):
    result = normalize_hear_result(result)
    segments = result.get("segments") or []
    safe_name = sanitize_filename(filename) if filename else None
    if org_id == DEFAULT_ORG_ID:
        from .auth import ensure_placeholder_org

        ensure_placeholder_org(conn)
    row = conn.execute(
        """
        INSERT INTO calls (
            org_id, audio_url, job_id, status, full_text, speakers, audio_seconds,
            raw_json, pyai_call_id, filename, source, external_id
        )
        VALUES (%s, %s, %s, 'completed', %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            org_id,
            identity,
            job_id,
            result.get("text", ""),
            result.get("speakers"),
            result.get("audio_seconds"),
            json.dumps(result),
            pyai_call_id,
            safe_name,
            source,
            str(external_id) if external_id is not None else None,
        ),
    ).fetchone()
    call_id = int(row["id"])
    for i, seg in enumerate(segments):
        conn.execute(
            """
            INSERT INTO segments (
                org_id, call_id, seq, speaker, channel, "start", "end", text
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                org_id,
                call_id,
                i,
                seg.get("speaker"),
                seg.get("channel"),
                seg.get("start"),
                seg.get("end"),
                seg.get("text"),
            ),
        )
    conn.commit()
    log.info(
        "saved transcript for call %d (%d segments, pyai_call_id=%s, filename=%s source=%s)",
        call_id, len(segments), pyai_call_id, safe_name, source,
    )
    return call_id


def replace_transcript(conn, call_id, job_id, result, pyai_call_id=None, *, org_id: str):
    """Overwrite Hear output for an existing call (re-transcribe)."""
    result = normalize_hear_result(result)
    segments = result.get("segments") or []
    conn.execute(
        "DELETE FROM segments WHERE call_id = %s AND org_id = %s",
        (call_id, org_id),
    )
    conn.execute(
        "DELETE FROM audits WHERE call_id = %s AND org_id = %s",
        (call_id, org_id),
    )
    updated = conn.execute(
        """
        UPDATE calls SET
            job_id = %s,
            status = 'completed',
            full_text = %s,
            speakers = %s,
            audio_seconds = %s,
            raw_json = %s,
            pyai_call_id = COALESCE(%s, pyai_call_id)
        WHERE id = %s AND org_id = %s
        """,
        (
            job_id,
            result.get("text", ""),
            result.get("speakers"),
            result.get("audio_seconds"),
            json.dumps(result),
            pyai_call_id,
            call_id,
            org_id,
        ),
    )
    if getattr(updated, "rowcount", 1) == 0:
        raise LookupError(f"call {call_id} not in org")
    for i, seg in enumerate(segments):
        conn.execute(
            """
            INSERT INTO segments (
                org_id, call_id, seq, speaker, channel, "start", "end", text
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                org_id,
                call_id,
                i,
                seg.get("speaker"),
                seg.get("channel"),
                seg.get("start"),
                seg.get("end"),
                seg.get("text"),
            ),
        )
    conn.commit()
    log.info(
        "replaced transcript for call %d (%d segments, job_id=%s)",
        call_id, len(segments), job_id,
    )
    return call_id


def set_filename_if_empty(conn, call_id, filename, *, org_id: str):
    """Backfill filename on deduped uploads when the row has no name yet."""
    if not filename:
        return
    safe = sanitize_filename(filename)
    conn.execute(
        """
        UPDATE calls SET filename = %s
        WHERE id = %s AND org_id = %s AND (filename IS NULL OR TRIM(filename) = '')
        """,
        (safe, call_id, org_id),
    )
    conn.commit()


def _separation_fields(mode: str | None = None, *, as_strings: bool = False, path: str | None = None) -> dict:
    """Exactly one of channel or diarize. Hear forbids sending both."""
    true = "true" if as_strings else True
    resolved = (mode or resolve_separation_mode(path)).strip().lower()
    if resolved == "diarize":
        return {"diarize": true}
    return {"channel": true}


# ---------- PyAI service wrapper ----------
def submit_job_url(audio_url, call_id=None):
    _require_api_key()
    body = {"audio_url": audio_url, "model": MODEL, "numerals": True, "output_formats": ["json"]}
    body.update(_separation_fields(mode="channel"))
    if call_id:
        body["call_id"] = call_id
        if RECAP_PACK_ID:
            body["pack_id"] = RECAP_PACK_ID
    idem = hashlib.sha256(audio_url.encode()).hexdigest()[:32]
    resp = pyai_usage.post(f"{BASE_URL}/v1/transcription/jobs", json=body,
                      headers={**HEADERS, "Idempotency-Key": idem}, timeout=60)
    return _job_id_from(resp)


# In-memory store for sync fallback results — keyed by synthetic job_id.
# Only populated when the async path is unavailable (sandbox keys).
_SYNC_RESULTS: dict = {}

_SYNC_SCOPE_HELP = (
    "This API key cannot use async Hear jobs (transcribe:jobs). "
    "CallProof needs speaker-labelled segments from POST /v1/transcription/jobs "
    "(diarize/channel). Sandbox keys only include hear:transcribe (text-only sync). "
    "Set a live PYAI_API_KEY with transcribe:jobs on the host and restart."
)


def submit_job_file(path, call_id=None, mode=None):
    """
    Submit audio for transcription.

    Tries the async jobs endpoint first (transcribe:jobs scope).
    If the key lacks that scope (403) — typical for sandbox keys —
    attempts the sync endpoint and only accepts a diarized segment payload
    compatible with the rest of CallProof. Text-only sync responses fail
    with a clear error instead of saving an empty transcript.
    """
    _require_api_key()
    resolved = (mode or resolve_separation_mode(path)).strip().lower()
    if resolved != "diarize":
        resolved = "channel"
    with open(path, "rb") as f:
        audio_bytes = f.read()

    files = {"audio": (os.path.basename(path), audio_bytes, "application/octet-stream")}
    data = {"model": MODEL, "numerals": "true", "output_formats": "json"}
    data.update(_separation_fields(resolved, as_strings=True))
    if call_id:
        data["call_id"] = call_id
        if RECAP_PACK_ID:
            data["pack_id"] = RECAP_PACK_ID

    applog.event(
        log, "hear_separation",
        mode=resolved, call_id=call_id, bytes=len(audio_bytes),
    )
    log.info("submitting %.2f MB to PyAI Hear (%s, call_id=%s)",
             len(audio_bytes) / 1_000_000, resolved, call_id)

    resp = pyai_usage.post(
        f"{BASE_URL}/v1/transcription/jobs",
        files=files, data=data, headers=HEADERS, timeout=120,
    )

    if resp.status_code == 403:
        log.warning(
            "async jobs returned 403 (missing transcribe:jobs) — "
            "trying sync endpoint for a diarized payload"
        )
        return _submit_sync_fallback(audio_bytes, path, data)

    return _job_id_from(resp)


def _normalize_sync_result(result: dict) -> dict:
    """Map sync/Whisper-shaped payloads toward the async jobs result shape."""
    out = dict(result)
    if not out.get("segments") and out.get("words"):
        out["segments"] = out.get("words") or []
    return normalize_hear_result(out)


def _has_usable_segments(result: dict) -> bool:
    segments = result.get("segments") or []
    if not segments:
        return False
    # Need at least one timed utterance with text; speaker preferred for QA.
    usable = 0
    speakers = set()
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        usable += 1
        if seg.get("speaker") is not None:
            speakers.add(seg.get("speaker"))
    return usable > 0 and len(speakers) >= 1


def _submit_sync_fallback(audio_bytes, path, data):
    """
    Sync fallback: POST /v1/audio/transcriptions (hear:transcribe scope).
    Returns a synthetic job_id only when the response includes speaker segments.
    """
    files = {"file": (os.path.basename(path), audio_bytes, "application/octet-stream")}
    sync_data = {
        "model": data.get("model", MODEL),
        "response_format": "verbose_json",
    }
    # Best-effort: some deployments accept these; ignored if unsupported.
    if "diarize" in data:
        sync_data["diarize"] = data["diarize"]
    if "channel" in data:
        sync_data["channel"] = data["channel"]
    if "numerals" in data:
        sync_data["numerals"] = data["numerals"]

    log.info("sync fallback: POST /v1/audio/transcriptions")
    resp = pyai_usage.post(
        f"{BASE_URL}/v1/audio/transcriptions",
        files=files, data=sync_data, headers=HEADERS, timeout=180,
    )

    if resp.status_code != 200:
        log.error("sync fallback rejected: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(
            f"PyAI sync transcription failed: {resp.status_code} {resp.text}. "
            f"{_SYNC_SCOPE_HELP}"
        )

    result = _normalize_sync_result(resp.json())
    if not _has_usable_segments(result):
        log.error(
            "sync fallback returned no speaker-labelled segments "
            "(keys=%s). CallProof cannot audit text-only transcripts.",
            sorted(result.keys()),
        )
        raise RuntimeError(_SYNC_SCOPE_HELP)

    call_id = data.get("call_id", "unknown")
    job_id = f"sync:{call_id}"
    _SYNC_RESULTS[job_id] = result
    log.info(
        "sync transcription usable (%d segments); synthetic job_id=%s",
        len(result.get("segments") or []), job_id,
    )
    return job_id


def _job_id_from(resp):
    if resp.status_code not in (200, 202):
        log.error("job submission rejected: %s %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"PyAI rejected the job: {resp.status_code} {resp.text}")
    job_id = resp.json().get("job_id")
    if not job_id:
        raise RuntimeError(f"No job_id in PyAI response: {resp.text}")
    log.info("job submitted: %s", job_id)
    return job_id


def poll_job(job_id):
    _require_api_key()
    # Sync fallback path — result already in memory, return immediately
    if job_id.startswith("sync:"):
        result = _SYNC_RESULTS.pop(job_id, None)
        if result is None:
            raise RuntimeError(f"Sync result for {job_id} not found (already consumed?)")
        log.info("sync job %s: returning inline result (no polling needed)", job_id)
        return result

    last_status = None
    budget_s = POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS
    log.info(
        "polling job %s (up to %ds, every %ds)",
        job_id, budget_s, POLL_INTERVAL_SECONDS,
    )
    for attempt in range(1, POLL_MAX_ATTEMPTS + 1):
        resp = pyai_usage.get(
            f"{BASE_URL}/v1/transcription/jobs/{job_id}", headers=HEADERS, timeout=30
        )
        if resp.status_code != 200:
            log.error("poll error: %s %s", resp.status_code, resp.text[:300])
            raise RuntimeError(f"PyAI poll error: {resp.status_code} {resp.text}")
        data = resp.json()
        status = data.get("status")
        if status != last_status:
            log.info("job %s status: %s (attempt %d)", job_id, status, attempt)
            last_status = status
        if status == "completed":
            return get_result(data)
        if status in ("failed", "cancelled"):
            reason = data.get("error") or data
            log.error("job %s %s: %s", job_id, status, reason)
            raise RuntimeError(f"PyAI job {status}: {reason}")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"PyAI job {job_id} did not finish within "
        f"{POLL_MAX_ATTEMPTS * POLL_INTERVAL_SECONDS}s"
    )


def get_result(job_data):
    if job_data.get("result"):
        return job_data["result"]
    if job_data.get("result_url"):
        r = pyai_usage.get(job_data["result_url"], timeout=30)
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Completed job has no result or result_url: {job_data}")


def _looks_like_tagged_dump(result: dict) -> bool:
    """Hear grouped both speakers into [speaker_1] … [speaker_2] … instead of turns."""
    segs = [s for s in (result.get("segments") or []) if isinstance(s, dict)]
    texts = [str(result.get("text") or "")]
    labeled = []
    for seg in segs:
        texts.append(str(seg.get("text") or ""))
        if seg.get("speaker") not in (None, ""):
            labeled.append(seg)
    blob = " ".join(texts)
    tags = {m.group(1).lower() for m in _SPEAKER_TAG.finditer(blob)}
    if len(tags) < 2:
        return False
    if len(segs) <= 1 or len(labeled) < 2:
        return True
    # Two long monologues after expanding a tagged blob — not interleaved turns.
    if len(segs) <= 2:
        durations = []
        for seg in segs:
            try:
                durations.append(
                    max(0.0, float(seg.get("end") or 0) - float(seg.get("start") or 0))
                )
            except (TypeError, ValueError):
                durations.append(0.0)
        if durations and min(durations) >= 15.0:
            return True
    return False


def speaker_split_ok(result: dict) -> bool:
    """True when Hear returned at least two labelled speakers as separate turns."""
    if not isinstance(result, dict) or _looks_like_tagged_dump(result):
        return False
    segs = [s for s in (result.get("segments") or []) if isinstance(s, dict)]
    labeled = [s for s in segs if s.get("speaker") not in (None, "")]
    speakers = _labeled_speaker_ids(labeled)
    return len(speakers) >= 2 and len(labeled) >= 2


def channel_split_ok(result: dict) -> bool:
    """True when Hear returned interleaved turns like v2testing-ui-final."""
    return speaker_split_ok(result)


def _separation_score(result: dict) -> tuple:
    """Prefer more speakers, then more turns. Penalize dumps and unlabeled blobs."""
    segs = [s for s in (result.get("segments") or []) if isinstance(s, dict)]
    labeled = [s for s in segs if s.get("speaker") not in (None, "")]
    n_spk = len(_labeled_speaker_ids(labeled))
    dump = 1 if _looks_like_tagged_dump(result) else 0
    unlabeled = 1 if n_spk < 2 else 0
    return (n_spk, len(labeled), -dump, -unlabeled)


def _hear_attempt(src_path, dest_path, call_id, mode: str):
    nch = 2 if mode == "channel" else 1
    if is_hear_wav(src_path, channels=nch):
        upload = src_path
    else:
        upload = make_hear_copy(
            src_path, dest_path, channels=nch, keep_if_larger=(mode == "channel"),
        ) or src_path
    job_id = submit_job_file(upload, call_id=call_id, mode=mode)
    result = normalize_hear_result(poll_job(job_id))
    return job_id, result


def transcribe_with_fallback(src_path, hear_tmp, call_id=None):
    """Split two-person calls: dual-channel → channel; mixed MP3 → diarize.

    Never keep an unlabeled one-speaker blob when the other mode has turns.
    """
    forced = (os.getenv("HEAR_SEPARATION_MODE") or SEPARATION_MODE or "auto").strip().lower()
    if forced == "diarize":
        job_id, result = _hear_attempt(src_path, hear_tmp, call_id, "diarize")
        return job_id, result, "diarize"

    stereo = has_true_stereo(src_path)
    channel_first = forced == "channel" or (forced != "diarize" and stereo)

    job_id = result = None
    if channel_first:
        job_id, result = _hear_attempt(src_path, hear_tmp, call_id, "channel")
        if speaker_split_ok(result):
            applog.event(
                log, "hear_separation_picked",
                mode="channel", call_id=call_id,
                speakers=result.get("speakers"),
                segments=len(result.get("segments") or []),
            )
            return job_id, result, "channel"
        applog.event(
            log, "hear_separation_retry",
            from_mode="channel", to_mode="diarize", call_id=call_id,
            segments=len(result.get("segments") or []),
            speakers=result.get("speakers"),
        )
        log.info("channel split missing turns; retrying Hear with diarize")

    mono_tmp = f"{hear_tmp}.mono.wav"
    try:
        job2, result2 = _hear_attempt(src_path, mono_tmp, call_id, "diarize")
    finally:
        if os.path.exists(mono_tmp):
            os.remove(mono_tmp)

    if result is None:
        picked_id, picked, mode = job2, result2, "diarize"
    elif _separation_score(result2) >= _separation_score(result):
        picked_id, picked, mode = job2, result2, "diarize"
    else:
        picked_id, picked, mode = job_id, result, "channel"

    if not speaker_split_ok(picked):
        applog.event(
            log, "hear_separation_unlabeled",
            mode=mode, call_id=call_id,
            speakers=picked.get("speakers"),
            segments=len(picked.get("segments") or []),
        )
        log.warning(
            "Hear returned no two-speaker turns (mode=%s, speakers=%s, segments=%s)",
            mode, picked.get("speakers"), len(picked.get("segments") or []),
        )
    else:
        applog.event(
            log, "hear_separation_picked",
            mode=mode, call_id=call_id,
            speakers=picked.get("speakers"),
            segments=len(picked.get("segments") or []),
        )
    return picked_id, picked, mode


def transcribe_audio(src_path, hear_tmp, call_id=None, *, org_id: str):
    """Dispatch to PyAI Hear or the self-hosted service (CL-40). Same result
    shape either way.

    org_id is the JWT/worker tenant, never a client-supplied id.
    Unset use_selfhosted_transcription stays on PyAI (Hear path untouched).

    The self-hosted engine runs on Cloud Run, not in this process — running
    torch/pyannote in-process here once starved/crashed the box serving live
    API traffic (see CL-40). transcribe_selfhosted.py (CL-38) still exists and
    is still tested; it's just not called from here anymore.
    """
    from . import org_features

    flags = org_features.features_for_org(org_id)
    if flags.get("use_selfhosted_transcription") is True:
        applog.event(log, "transcribe_dispatch", engine="selfhosted", call_id=call_id)
        from .transcribe_selfhosted_remote import transcribe_remote

        return transcribe_remote(src_path, call_id=call_id)
    applog.event(log, "transcribe_dispatch", engine="pyai", call_id=call_id)
    return transcribe_with_fallback(src_path, hear_tmp, call_id=call_id)


# ---------- Orchestrator (CLI) ----------
def identity_for(src):
    if is_url(src):
        return src
    with open(src, "rb") as f:
        return "file-sha256:" + hashlib.sha256(f.read()).hexdigest()[:24]


def main(argv: list[str] | None = None):
    argv = list(sys.argv if argv is None else argv)
    if not PYAI_API_KEY:
        sys.exit("ERROR: PYAI_API_KEY not found. Set it on the host environment.")
    src = (argv[1] if len(argv) > 1 else "").strip() or (os.getenv("CALLPROOF_CLI_AUDIO") or "").strip()
    if not src:
        sys.exit(
            "ERROR: pass an audio file path "
            "(python transcribe.py /path/to.mp3) or set CALLPROOF_CLI_AUDIO"
        )
    init_db()
    if not is_url(src) and not os.path.isfile(src):
        sys.exit(f"ERROR: file not found: {src}")
    identity = identity_for(src)
    with org_scope(DEFAULT_ORG_ID):
        with db.connection() as conn:
            existing = find_existing_call(conn, identity, org_id=DEFAULT_ORG_ID)
            if existing:
                log.info(
                    "already transcribed (call id %d) - loading from DB, no API call",
                    existing["id"],
                )
                return
            pyai_id = new_pyai_call_id()
            hear_tmp = None
            try:
                if is_url(src):
                    job_id = submit_job_url(src, call_id=pyai_id)
                    result = poll_job(job_id)
                else:
                    hear_tmp = src + ".hear-tmp.wav"
                    job_id, result, _mode = transcribe_with_fallback(
                        src, hear_tmp, call_id=pyai_id,
                    )
                call_id = save_transcript(
                    conn, identity, job_id, result,
                    pyai_call_id=pyai_id,
                    filename=None if is_url(src) else os.path.basename(src),
                    org_id=DEFAULT_ORG_ID,
                )
                log.info("done: call id %d", call_id)
            finally:
                if hear_tmp and os.path.exists(hear_tmp):
                    os.remove(hear_tmp)


if __name__ == "__main__":
    main()