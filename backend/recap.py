"""
CallProof - PyAI Recap client.

Recap does not transcribe; it turns a speaker-labelled utterance list into a
TL;DR, summary, and action items. Requires the Recap add-on plus recap:read
(and usually recap:configure to enable the org). Failures are soft — audits
still succeed without Recap.
"""

from __future__ import annotations

import logging
import os
import time

import httpx
from . import applog
from . import pyai_usage
from .config import load_env

load_env()
applog.setup_logging()

log = logging.getLogger("callproof.recap")

PYAI_API_KEY = (os.getenv("PYAI_API_KEY") or "").strip() or None
BASE_URL = "https://api.pyai.com"
RECAP_PACK_ID = os.getenv("RECAP_PACK_ID") or None
RECAP_POLL_INTERVAL = 2
RECAP_POLL_ATTEMPTS = 45  # ~90s

HEADERS = {"Authorization": f"Bearer {PYAI_API_KEY}"} if PYAI_API_KEY else {}

SANDBOX_RECAP_ERROR = (
    "Recap is unavailable on a sandbox PyAI key. "
    "Create a live API key at https://console.pyai.com and set PYAI_API_KEY "
    "in your .env, then restart and re-run the audit."
)


def set_api_key(api_key: str):
    """Inject / rotate the PyAI key at runtime (used by api.py sandbox mint)."""
    global PYAI_API_KEY, HEADERS
    key = (api_key or "").strip()
    if not key:
        raise ValueError("PYAI_API_KEY cannot be empty")
    PYAI_API_KEY = key
    HEADERS = {"Authorization": f"Bearer {key}"}
    os.environ["PYAI_API_KEY"] = key


def is_sandbox_key(api_key: str | None = None) -> bool:
    key = (api_key if api_key is not None else PYAI_API_KEY) or ""
    return key.startswith("pyai_test_")


def sandbox_recap_unavailable():
    return {
        "status": "unavailable",
        "reason": "sandbox_key",
        "error": SANDBOX_RECAP_ERROR,
    }


def pyai_call_id_for(local_call_id: int, stored: str | None = None) -> str:
    return stored or f"callproof-{local_call_id}"


def segments_to_utterances(segments, agent_speaker):
    """Map CallProof segments to Recap utterances (agent/customer roles)."""
    out = []
    for s in segments:
        start = float(s.get("start") or 0)
        end = float(s.get("end") or start)
        text = (s.get("text") or "").strip()
        if not text:
            continue
        role = "agent" if s.get("speaker") == agent_speaker else "customer"
        out.append({
            "speaker_role": role,
            "text": text,
            "offset_s": start,
            "duration_s": max(0.0, end - start),
        })
    return out


def _normalize(payload: dict) -> dict:
    record = payload.get("record") or {}
    if not isinstance(record, dict):
        record = {}
    action_items = record.get("action_items") or []
    if not isinstance(action_items, list):
        action_items = []
    return {
        "status": "ok",
        "pyai_call_id": payload.get("call_id"),
        "recap_status": payload.get("status"),
        "headline": payload.get("headline") or record.get("tldr") or "",
        "tldr": record.get("tldr") or payload.get("headline") or "",
        "summary": record.get("summary") or record.get("summary_draft") or "",
        "action_items": [
            {
                "owner": (it or {}).get("owner"),
                "task": (it or {}).get("task"),
                "due": (it or {}).get("due"),
            }
            for it in action_items
            if isinstance(it, dict)
        ],
        "error": payload.get("error"),
    }


def get_recap(pyai_call_id: str):
    """GET /v1/recap/calls/{id}. Returns (status_code, json_or_none)."""
    resp = pyai_usage.get(
        f"{BASE_URL}/v1/recap/calls/{pyai_call_id}",
        headers=HEADERS,
        timeout=30,
    )
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001
        body = None
    return resp.status_code, body


def trigger_recap(pyai_call_id: str, utterances, audio_seconds=None):
    """POST utterances to start Recap for an existing transcript."""
    body = {"utterances": utterances}
    if audio_seconds is not None:
        body["call_duration_s"] = float(audio_seconds)
    if RECAP_PACK_ID:
        body["pack_id"] = RECAP_PACK_ID
    resp = pyai_usage.post(
        f"{BASE_URL}/v1/recap/calls/{pyai_call_id}",
        headers={**HEADERS, "Content-Type": "application/json"},
        json=body,
        timeout=60,
    )
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001
        data = {"raw": resp.text[:400]}
    return resp.status_code, data


def poll_recap(pyai_call_id: str):
    last = None
    for attempt in range(1, RECAP_POLL_ATTEMPTS + 1):
        code, data = get_recap(pyai_call_id)
        if code == 404:
            return None
        if code == 402:
            log.warning("recap payment required for %s", pyai_call_id)
            return {"status": "unavailable", "error": "Recap requires prepaid credit or the Recap add-on."}
        if code == 401 or code == 403:
            msg = (data or {}).get("error", {}).get("message") if isinstance(data, dict) else None
            log.warning("recap auth/scope error %s: %s", code, msg or data)
            return {
                "status": "unavailable",
                "error": msg or "Recap not enabled for this API key (need recap:read / Recap add-on).",
            }
        if code != 200 or not isinstance(data, dict):
            log.warning("recap poll unexpected %s: %s", code, data)
            return {"status": "error", "error": f"Recap poll failed ({code})."}

        status = data.get("status")
        if status != last:
            log.info("recap %s status: %s (attempt %d)", pyai_call_id, status, attempt)
            last = status
        if status == "complete":
            return _normalize(data)
        if status == "failed":
            return {
                "status": "error",
                "error": data.get("error") or "Recap processing failed.",
                "pyai_call_id": pyai_call_id,
            }
        time.sleep(RECAP_POLL_INTERVAL)

    return {
        "status": "pending",
        "error": "Recap still processing; try again shortly.",
        "pyai_call_id": pyai_call_id,
    }


def ensure_recap(local_call_id, segments, agent_speaker, audio_seconds=None, stored_pyai_id=None):
    """Fetch Recap for this call, triggering from utterances when needed."""
    if not PYAI_API_KEY:
        applog.event(
            log, "recap_failure", level=logging.WARNING,
            call_id=local_call_id, error="PYAI_API_KEY not configured",
        )
        return {"status": "unavailable", "error": "PYAI_API_KEY not configured."}

    # Sandbox keys do not include Recap scopes — fail fast with a clear message.
    if is_sandbox_key():
        applog.event(
            log, "recap_failure", level=logging.WARNING,
            call_id=local_call_id, error="sandbox_key",
        )
        return sandbox_recap_unavailable()

    pyai_id = pyai_call_id_for(local_call_id, stored_pyai_id)
    code, existing = get_recap(pyai_id)
    if code == 200 and isinstance(existing, dict):
        if existing.get("status") == "complete":
            log.info("recap HIT for %s", pyai_id)
            result = _normalize(existing)
            applog.event(
                log, "recap_success",
                call_id=local_call_id, pyai_call_id=pyai_id, source="cache",
            )
            return result
        if existing.get("status") in ("pending", "processing"):
            result = poll_recap(pyai_id)
            _log_recap_outcome(local_call_id, pyai_id, result, source="poll")
            return result
        if existing.get("status") == "failed":
            # Retry once with a fresh utterance submit
            log.info("recap prior failed for %s; re-triggering", pyai_id)

    utterances = segments_to_utterances(segments, agent_speaker)
    if not utterances:
        applog.event(
            log, "recap_failure", level=logging.ERROR,
            call_id=local_call_id, pyai_call_id=pyai_id,
            error="No utterances available for Recap",
        )
        return {"status": "error", "error": "No utterances available for Recap."}

    t_code, t_body = trigger_recap(pyai_id, utterances, audio_seconds)
    if t_code not in (200, 202):
        msg = None
        if isinstance(t_body, dict):
            err = t_body.get("error")
            if isinstance(err, dict):
                msg = err.get("message")
            elif isinstance(err, str):
                msg = err
            msg = msg or t_body.get("detail") or t_body.get("title")
        log.warning("recap trigger failed %s: %s", t_code, t_body)
        # Scope/auth failures on a test key → same clear sandbox guidance.
        if t_code in (401, 403) and is_sandbox_key():
            applog.event(
                log, "recap_failure", level=logging.WARNING,
                call_id=local_call_id, pyai_call_id=pyai_id,
                http_status=t_code, error="sandbox_key",
            )
            return sandbox_recap_unavailable()
        applog.event(
            log, "recap_failure", level=logging.ERROR,
            call_id=local_call_id, pyai_call_id=pyai_id,
            http_status=t_code, error=msg or f"trigger_failed_{t_code}",
        )
        if t_code in (401, 403, 402):
            return {
                "status": "unavailable",
                "error": msg or "Recap add-on not enabled for this organization / key.",
            }
        return {"status": "error", "error": msg or f"Recap trigger failed ({t_code})."}

    log.info("recap triggered for %s (%s)", pyai_id, t_code)
    result = poll_recap(pyai_id)
    _log_recap_outcome(local_call_id, pyai_id, result, source="trigger")
    return result


def _log_recap_outcome(local_call_id, pyai_id, result, source):
    status = (result or {}).get("status")
    if status == "ok":
        applog.event(
            log, "recap_success",
            call_id=local_call_id, pyai_call_id=pyai_id, source=source,
        )
    else:
        applog.event(
            log, "recap_failure", level=logging.WARNING,
            call_id=local_call_id, pyai_call_id=pyai_id, source=source,
            status=status, error=(result or {}).get("error"),
        )
