"""HTTP client for the self-hosted transcription service (CL-40, GCP Cloud Run).

Render calls this instead of running torch/pyannote in-process — that took
down the whole API once already by starving/exhausting the box's resources.
Same (job_id, result, mode) return shape as transcribe_selfhosted() and
transcribe_with_fallback(), so transcribe_audio()'s dispatch doesn't care
which one ran.

Auth: a Google-signed ID token minted from a service account key (never a
shared secret), scoped to the Cloud Run service's own URL. Cloud Run itself
rejects anything without a valid token before this code's request even lands.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

import httpx

from . import applog

log = logging.getLogger("callproof.transcribe_selfhosted_remote")

REQUEST_TIMEOUT = 600  # ~200s measured processing time, plus real headroom
_TOKEN_LIFETIME_S = 3000  # Google ID tokens last ~1h; refresh a bit early

_token_cache: dict = {"token": None, "exp": 0.0}
_lock = threading.Lock()


def _service_url() -> str:
    url = (os.getenv("SELFHOSTED_TRANSCRIBE_URL") or "").strip().rstrip("/")
    if not url:
        raise RuntimeError("SELFHOSTED_TRANSCRIBE_URL is not set.")
    return url


def _service_account_info() -> dict:
    raw = (os.getenv("SELFHOSTED_GCP_SERVICE_ACCOUNT_JSON") or "").strip()
    if not raw:
        raise RuntimeError("SELFHOSTED_GCP_SERVICE_ACCOUNT_JSON is not set.")
    return json.loads(raw)


def _fetch_id_token(audience: str) -> str:
    from google.auth.transport.requests import Request as GoogleAuthRequest
    from google.oauth2 import service_account

    info = _service_account_info()
    creds = service_account.IDTokenCredentials.from_service_account_info(
        info, target_audience=audience,
    )
    creds.refresh(GoogleAuthRequest())
    return creds.token


def reset_token_cache() -> None:
    """Tests only."""
    with _lock:
        _token_cache["token"] = None
        _token_cache["exp"] = 0.0


def _get_id_token(audience: str) -> str:
    with _lock:
        now = time.time()
        if _token_cache["token"] and now < _token_cache["exp"]:
            return _token_cache["token"]
        token = _fetch_id_token(audience)
        _token_cache["token"] = token
        _token_cache["exp"] = now + _TOKEN_LIFETIME_S
        return token


def transcribe_remote(src_path: str, call_id=None) -> tuple[str, dict, str]:
    """POST the audio to the Cloud Run self-hosted service.

    Same return contract as transcribe_selfhosted()/transcribe_with_fallback():
    (job_id, result, mode).
    """
    url = _service_url()
    token = _get_id_token(url)
    t0 = time.perf_counter()
    with open(src_path, "rb") as f:
        files = {"audio": (os.path.basename(src_path), f, "application/octet-stream")}
        resp = httpx.post(
            f"{url}/transcribe",
            files=files,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT,
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
