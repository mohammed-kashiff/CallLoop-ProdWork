"""
JustCall client for completed-call ingest.

Downloads recordings only via api.justcall.io (no arbitrary URL fetch).
Never logs API secrets or full phone numbers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
from datetime import datetime, timedelta
from typing import Any

import httpx

from . import applog

log = logging.getLogger("callproof.justcall")

BASE_URL = "https://api.justcall.io"
MAX_RECORDING_BYTES = 25 * 1024 * 1024


def api_key() -> str:
    return (os.getenv("JUSTCALL_API_KEY") or "").strip()


def api_secret() -> str:
    return (os.getenv("JUSTCALL_API_SECRET") or "").strip()


def set_credentials(api_key: str, api_secret_value: str) -> None:
    """Apply JustCall credentials for this process. Does not log the values."""
    os.environ["JUSTCALL_API_KEY"] = (api_key or "").strip()
    os.environ["JUSTCALL_API_SECRET"] = (api_secret_value or "").strip()


def webhook_secret() -> str:
    return (os.getenv("JUSTCALL_WEBHOOK_SECRET") or "").strip()


def configured() -> bool:
    return bool(api_key() and api_secret())


def poll_seconds() -> int:
    raw = (os.getenv("JUSTCALL_POLL_SECONDS") or "45").strip()
    try:
        return max(15, min(int(raw), 3600))
    except ValueError:
        return 45


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"{api_key()}:{api_secret()}",
        "Accept": "application/json",
    }


def _require_configured() -> None:
    if not configured():
        raise RuntimeError(
            "JustCall is not configured. Set JUSTCALL_API_KEY and "
            "JUSTCALL_API_SECRET in .env (from JustCall → APIs and Webhooks)."
        )


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """
    If JUSTCALL_WEBHOOK_SECRET is set, require a matching HMAC hex digest.
    If unset, URL-validation pings from JustCall still return 200 (local demo).
    """
    secret = webhook_secret()
    if not secret:
        return True
    if not signature:
        return False
    sig = signature.strip()
    if sig.lower().startswith("sha256="):
        sig = sig[7:]
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    try:
        return hmac.compare_digest(digest, sig)
    except (TypeError, ValueError):
        return False


def extract_completed_id(payload: dict[str, Any]) -> str | None:
    """Return a JustCall call id from a webhook/list payload, or None."""
    if not isinstance(payload, dict):
        return None
    event = str(payload.get("type") or payload.get("event") or "").lower()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    if event in ("url.validation", "webhook.validation", "validation"):
        return None
    if event and event not in (
        "call.completed",
        "call_completed",
        "call.updated",
        "call.ended",
        "",
    ):
        if event.startswith("call.") and "complet" not in event:
            return None
    cid = data.get("id") or data.get("call_id") or payload.get("id")
    if cid is None or cid == "":
        return None
    return str(cid).strip()


def display_name(payload: dict[str, Any], call_id: str) -> str:
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("data") if isinstance(data.get("data"), dict) else data
    agent_obj = nested.get("agent") if isinstance(nested.get("agent"), dict) else {}
    contact_obj = nested.get("contact") if isinstance(nested.get("contact"), dict) else {}
    agent = str(
        nested.get("agent_name")
        or agent_obj.get("name")
        or agent_obj.get("email")
        or ""
    ).strip()
    contact = str(
        nested.get("contact_name")
        or contact_obj.get("name")
        or ""
    ).strip()
    label = " · ".join(p for p in (agent, contact) if p) or f"JustCall {call_id}"
    return f"justcall-{call_id}-{label}"[:160]


def list_recent_calls(hours: int = 24, limit: int = 30) -> list[dict[str, Any]]:
    _require_configured()
    hours = max(1, min(int(hours), 72))
    limit = max(1, min(int(limit), 50))
    start = datetime.now() - timedelta(hours=hours)
    from_dt = start.strftime("%Y-%m-%d %H:%M:%S")
    with httpx.Client(timeout=30.0) as client:
        r = client.get(
            f"{BASE_URL}/v2.1/calls",
            headers=_headers(),
            params={
                "from_datetime": from_dt,
                "page": 0,
                "per_page": limit,
            },
        )
    if r.status_code == 401:
        raise RuntimeError("JustCall rejected the API credentials (401).")
    r.raise_for_status()
    body = r.json() if r.content else {}
    rows = body.get("data") or body.get("calls") or body.get("results") or []
    if isinstance(body, list):
        rows = body
    out: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
    return out


def download_recording(call_id: str) -> bytes | None:
    """
    Fetch the recording through JustCall's download API.
    Returns None if the recording is not ready yet (404).
    """
    _require_configured()
    cid = str(call_id).strip()
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        raise ValueError("Invalid JustCall call id.")
    url = f"{BASE_URL}/v2.1/calls/{cid}/recording/download"
    with httpx.Client(timeout=60.0, follow_redirects=True) as client:
        r = client.get(url, headers=_headers())
    if r.status_code in (404, 403):
        applog.event(
            log, "justcall_recording_pending",
            justcall_id=cid, status=r.status_code,
        )
        return None
    if r.status_code == 401:
        raise RuntimeError("JustCall rejected the API credentials (401).")
    r.raise_for_status()
    data = r.content or b""
    ctype = (r.headers.get("content-type") or "").lower()
    if "json" in ctype or data[:1] in (b"{", b"["):
        applog.event(log, "justcall_recording_pending", justcall_id=cid, status="json")
        return None
    if len(data) > MAX_RECORDING_BYTES:
        raise RuntimeError("JustCall recording exceeds the 25 MB upload limit.")
    if not data:
        return None
    return data


def identity_for(call_id: str) -> str:
    return f"justcall:{str(call_id).strip()}"


def recording_suffix(data: bytes) -> str:
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return ".wav"
    if data[:4] == b"fLaC":
        return ".flac"
    if len(data) >= 8 and data[4:8] == b"ftyp":
        return ".m4a"
    return ".mp3"
