"""HTTP client for the self-hosted transcription service (CL-40, Modal GPU).

Render calls this instead of running torch/pyannote in-process — that took
down the whole API once already by starving/exhausting the box's resources.
Same (job_id, result, mode) return shape as transcribe_selfhosted() and
transcribe_with_fallback(), so transcribe_audio()'s dispatch doesn't care
which one ran.

No auth token: Modal's web endpoint is a plain public HTTPS URL, unlike the
Google-IAM-gated Cloud Run service this replaced.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

from . import applog

log = logging.getLogger("callproof.transcribe_selfhosted_remote")

REQUEST_TIMEOUT = 1800  # Modal enforces a 150s HTTP timeout per hop and
# returns a 303 redirect to a polling URL for anything slower; follow_redirects
# below handles that automatically. 1800s is the overall ceiling across all
# of those hops, matching the Cloud Run figure this replaced.


def _service_url() -> str:
    url = (os.getenv("SELFHOSTED_TRANSCRIBE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SELFHOSTED_TRANSCRIBE_URL is not set.")
    return url


def transcribe_remote(src_path: str, call_id=None) -> tuple[str, dict, str]:
    """POST the audio to the Modal self-hosted service.

    Same return contract as transcribe_selfhosted()/transcribe_with_fallback():
    (job_id, result, mode).
    """
    url = _service_url()
    t0 = time.perf_counter()
    with open(src_path, "rb") as f:
        files = {"audio": (os.path.basename(src_path), f, "application/octet-stream")}
        resp = httpx.post(
            f"{url}/transcribe",
            files=files,
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
    if resp.status_code != 200:
        applog.event(
            log, "selfhosted_remote_failed",
            call_id=call_id if call_id is not None else "-",
            status=resp.status_code,
        )
        raise RuntimeError(
            f"Self-hosted transcription service returned {resp.status_code}: "
            f"{resp.text[:300]}"
        )
    body = resp.json()
    job_id = str(body.get("job_id") or f"selfhosted_{int(time.time())}")
    result = body.get("result") or {}
    mode = body.get("mode") or "selfhosted"
    applog.event(
        log, "selfhosted_remote_transcribe",
        call_id=call_id if call_id is not None else "-",
        ms=int((time.perf_counter() - t0) * 1000),
        segments=len((result or {}).get("segments") or []),
    )
    return job_id, result, mode
