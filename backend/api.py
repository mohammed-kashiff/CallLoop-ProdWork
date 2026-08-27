"""
CallProof - FastAPI backend (v3, with logging).

Every request logs what it does. Crucially, /audit logs whether it served from
CACHE (stable score) or recomputed (MISS) - so you can see, per request, why a
score is or isn't changing.

App-owned credentials (PYAI_API_KEY, ANTHROPIC_API_KEY, SUPABASE_SERVICE_ROLE_KEY)
come from the host environment. This process does not write them to .env or
Postgres. CallProof QA needs a live PyAI key with transcribe:jobs for diarized
Hear jobs — sandbox keys are hear:transcribe only.
"""

from __future__ import annotations

import os
import csv
import io
import re
import json
import hashlib
import logging
import tempfile
import time
import uuid
import shutil
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from xml.sax.saxutils import escape as xml_escape

import httpx
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from . import applog
from . import audit_store
from . import audio_store
from . import auth
from . import db
from . import env_keys
from . import error_notify
from . import justcall
from . import org_vault
from . import sentry_report
from .config import cors_origins, load_env, skip_startup

load_env()
applog.setup_logging()
sentry_report.init_sentry()

from . import cost_estimate
from . import email_notify
from . import pyai_usage
from . import qa_engine as qa
from . import qa_v8
from . import recap as pyai_recap
from . import transcribe
from .org_ids import DEFAULT_RUBRIC_ID, integration_org_id, org_scope

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.api")
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BULK_FILES = 100
MAX_BULK_WORKERS = 20
MAX_BATCH_ZIP_BYTES = MAX_UPLOAD_BYTES * MAX_BULK_FILES
AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".webm", ".mpeg", ".mpga", ".aac"}
_db_lock = threading.Lock()
_justcall_poller_started = False

app = FastAPI(title="CallProof API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(auth.JwtAuthMiddleware)


_HEALTH_PATHS = frozenset({"/", "/health", "/healthz"})
_PYAI_ME_TTL_SEC = 120.0
_pyai_me_lock = threading.Lock()
_pyai_me_cache: dict[str, object] = {"key": "", "at": 0.0, "body": None, "status": 0}


def _pyai_me_cached(key: str, *, allow_stale: bool = False) -> tuple[int, dict] | None:
    with _pyai_me_lock:
        if _pyai_me_cache.get("key") != key:
            return None
        age = time.monotonic() - float(_pyai_me_cache.get("at") or 0)
        if not allow_stale and age > _PYAI_ME_TTL_SEC:
            return None
        body = _pyai_me_cache.get("body")
        status = int(_pyai_me_cache.get("status") or 0)
        if not isinstance(body, dict) or status < 1:
            return None
        return status, body


def _pyai_me_store(key: str, status: int, body: dict) -> None:
    with _pyai_me_lock:
        _pyai_me_cache["key"] = key
        _pyai_me_cache["at"] = time.monotonic()
        _pyai_me_cache["body"] = body
        _pyai_me_cache["status"] = status


def _pyai_me_clear() -> None:
    with _pyai_me_lock:
        _pyai_me_cache["key"] = ""
        _pyai_me_cache["at"] = 0.0
        _pyai_me_cache["body"] = None
        _pyai_me_cache["status"] = 0


@app.middleware("http")
async def log_http(request: Request, call_next):
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as e:  # noqa: BLE001
        duration_ms = (time.perf_counter() - started) * 1000
        err = f"{type(e).__name__}: {e}"
        applog.event(
            log, "http_error",
            level=logging.ERROR,
            method=request.method,
            path=request.url.path,
            duration_ms=round(duration_ms, 1),
            error=err,
        )
        error_notify.notify_http_error(
            method=request.method,
            path=request.url.path,
            status=500,
            error=err,
            duration_ms=round(duration_ms, 1),
        )
        raise

    duration_ms = (time.perf_counter() - started) * 1000
    status = response.status_code
    if request.url.path in _HEALTH_PATHS and status < 400:
        return response
    fields = {
        "method": request.method,
        "path": request.url.path,
        "status": status,
        "duration_ms": round(duration_ms, 1),
    }
    if status >= 400:
        applog.event(log, "http_error", level=logging.ERROR, **fields)
        if status >= 500:
            error_notify.notify_http_error(
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round(duration_ms, 1),
            )
    else:
        applog.event(log, "http_request", **fields)
    return response


@app.get("/health")
@app.get("/healthz")
@app.get("/")
def health():
    """Liveness for hosts (Render). Does not call PyAI, JustCall, or Claude."""
    return {"ok": True}


@app.get("/api/me")
def me(request: Request):
    """Current user from the verified JWT + org_members row."""
    org_id = _org(request)
    return {
        "user_id": getattr(request.state, "user_id", None),
        "org_id": org_id,
        "role": getattr(request.state, "role", None),
    }


def _conn():
    return db.connection()


def _org(request: Request) -> str:
    """JWT membership only. Never query, body, or path."""
    return auth.org_id_from_request(request)


def _apply_pyai_key(api_key: str):
    """Push a key into the process + both PyAI client modules."""
    transcribe.set_api_key(api_key)
    pyai_recap.set_api_key(api_key)


# ── Startup ───────────────────────────────────────────────────────────────────
def _startup():
    pyai_key = (os.environ.get("PYAI_API_KEY") or "").strip()
    if not pyai_key:
        log.warning(
            "PYAI_API_KEY is not set.\n"
            "   ➤ Set it on the host (Render env, or a gitignored local .env).\n"
            "   ➤ This process does not write secrets to .env or the database."
        )
    else:
        _apply_pyai_key(pyai_key)
        kind = "sandbox" if pyai_key.startswith("pyai_test_") else "configured"
        log.info("PYAI_API_KEY present (%s key)", kind)
        if kind == "sandbox":
            log.warning(
                "Using a sandbox key: async diarized jobs and Recap are likely "
                "unavailable. Prefer a live key with transcribe:jobs + recap:read."
            )

    if not (os.environ.get("ANTHROPIC_API_KEY") or "").strip():
        log.warning(
            "ANTHROPIC_API_KEY is not set.\n"
            "   ➤ The QA engine needs this on the host environment."
        )

    transcribe.init_db()
    if not audio_store.configured():
        log.warning(
            "SUPABASE_SERVICE_ROLE_KEY is not set.\n"
            "   ➤ Uploads and playback need private Storage.\n"
            "   ➤ Set the service role key on the host (never a VITE_* var)."
        )
    pyai_usage.init_usage_db()
    error_notify.log_ready()
    log.info("startup complete; db=postgres audit_mode=%s claude_model=%s", qa.audit_mode(), qa.MODEL)


if not skip_startup():
    _startup()


# ── Helpers ───────────────────────────────────────────────────────────────────
def _rubric_hash():
    with open(qa.RUBRIC_PATH, "rb") as f:
        body = f.read()
    # Bust cache when scoring policy changes (hybrid vs full).
    body += (
        f"\naudit_mode={qa.audit_mode()}\nrole=channel\nmodel={qa.MODEL}\n"
        f"claude_effort={getattr(qa, 'CLAUDE_EFFORT', 'high')}\n"
        f"rules_rev=tone_banks_v3\n"
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()[:16]


def _call_filename(call_id: int, org_id: str) -> str:
    with _conn() as c:
        row = c.execute(
            "SELECT filename FROM calls WHERE id = %s AND org_id = %s",
            (call_id, org_id),
        ).fetchone()
    name = (row["filename"] if row else None) or ""
    name = name.strip()
    return name or f"call-{call_id}.mp3"


def _attach_filename(audit: dict, call_id: int, org_id: str) -> dict:
    out = dict(audit)
    out["call_id"] = call_id
    out["filename"] = _call_filename(call_id, org_id)
    return out



def analyze_call(call_id, org_id: str, agent_override=None):
    started = time.perf_counter()
    applog.event(log, "audit_started", call_id=call_id)

    with _conn() as c:
        exists = c.execute(
            """
            SELECT 1 FROM calls
            WHERE id = %s AND org_id = %s AND status='completed'
            """,
            (call_id, org_id),
        ).fetchone()
    if not exists:
        applog.event(
            log, "audit_failed", level=logging.ERROR,
            call_id=call_id, error="call_not_found_or_incomplete",
        )
        raise HTTPException(
            status_code=404, detail=f"No completed call with id {call_id}"
        )

    call_id, meta, segments = qa.load_call(call_id, org_id=org_id)
    if not segments:
        applog.event(
            log, "audit_failed", level=logging.ERROR,
            call_id=call_id, error="no_segments",
        )
        raise HTTPException(
            status_code=422, detail=f"Call {call_id} has no segments"
        )

    agent = agent_override or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    with open(qa.RUBRIC_PATH) as f:
        rubric = json.load(f)

    mode = qa.audit_mode()
    is_v8 = qa_v8.is_v8_rubric(rubric)
    if is_v8:
        n_items = len(qa_v8.list_dimensions(rubric))
        log.info(
            "computing audit for call %d (%d v8 dimensions, mode=%s)",
            call_id, n_items, mode,
        )
        criteria_arg = []
    else:
        n_items = len(rubric["criteria"])
        log.info(
            "computing audit for call %d (%d criteria, mode=%s)",
            call_id, n_items, mode,
        )
        criteria_arg = rubric["criteria"]

    # One parallel wave: dimensions/criteria + churn + Recap.
    # Retention email and areas of improvement are on-demand.
    with ThreadPoolExecutor(max_workers=2) as pool:
        wave_f = pool.submit(
            qa.run_parallel_claude_wave,
            criteria_arg, segments, agent, transcript_text,
            None, rubric,
        )
        recap_f = pool.submit(
            pyai_recap.ensure_recap,
            call_id, segments, agent,
            meta.get("audio_seconds"), meta.get("pyai_call_id"),
        )
        wave = wave_f.result()
        try:
            call_recap = recap_f.result()
        except Exception as e:  # noqa: BLE001
            log.error("recap failed for call %d: %s", call_id, e)
            applog.event(
                log, "recap_failure", level=logging.ERROR,
                call_id=call_id, error=str(e),
            )
            call_recap = {"status": "error", "error": str(e)}

    churn = wave.get("churn")
    feedback = wave.get("feedback")
    manager_review = wave.get("manager_review") or []
    # On-demand — drafted when Email stakeholder is used
    retention_email = {"status": "pending"}

    if wave.get("mode") == "v8":
        score = wave["score"]
        grade = wave["grade"]
        tally = wave["tally"]
        findings = wave["findings"]
        gate_fails = [t.get("reason", "manager_review") for t in manager_review]
        flagged = bool(manager_review)
    else:
        results = wave["results"]
        _rows, score, _e, _p, tally, gate_fails = qa.score_results(results)
        grade = qa.performance_band(score, rubric)
        findings = [
            {
                "id": cr["id"], "name": cr["name"], "method": cr["method"],
                "weight": cr["weight"], "is_gate": bool(cr.get("is_gate")),
                "verdict": res["verdict"], "reasoning": res.get("reasoning", ""),
                "why": (
                    f"{cr['name']}: {(res.get('verdict') or '').title()} — "
                    f"{qa.awarded_points(cr, res['verdict']) if qa.awarded_points(cr, res['verdict']) is not None else '—'} "
                    f"of {cr['weight']} points. {(res.get('reasoning') or '').strip()}"
                ).strip(),
                "points": qa.awarded_points(cr, res["verdict"]),
                "evidence_text": res.get("evidence_text"),
                "evidence_seq": res.get("evidence_seq"),
                "evidence_verified": res.get("evidence_verified"),
            }
            for cr, res in results
        ]
        flagged = bool(gate_fails)

    duration_ms = (time.perf_counter() - started) * 1000
    applog.event(
        log, "audit_completed",
        call_id=call_id,
        score=score,
        grade=grade,
        duration_ms=round(duration_ms, 1),
        recap_status=(call_recap or {}).get("status"),
        flagged=flagged,
        manager_review_count=len(manager_review),
        audit_mode=qa.audit_mode(),
        agent_speaker=agent,
    )

    return {
        "call_id": call_id,
        "filename": _call_filename(call_id, org_id),
        "audio_seconds": meta.get("audio_seconds"),
        "agent_speaker": agent, "rubric": rubric["name"],
        "rubric_id": rubric.get("rubric_id") or rubric.get("name"),
        "score": score, "grade": grade, "tally": tally,
        "gate_fails": gate_fails, "flagged": flagged,
        "manager_review": manager_review,
        "segments": segments, "findings": findings,
        "churn": churn, "feedback": feedback,
        "retention_email": retention_email, "recap": call_recap,
        "audit_mode": qa.audit_mode(),
    }


def _load_or_compute_audit(call_id: int, org_id: str, refresh: bool = False):
    """Return (audit_dict, rubric_hash). Computes and caches on miss/refresh.

    Read mode: latest-per-rubric (default legacy v8).
    """
    rh = _rubric_hash()
    prev = None
    prev_hash = None
    with _db_lock:
        with _conn() as c:
            row = audit_store.fetch_latest_for_rubric(
                c,
                call_id=call_id,
                rubric_id=DEFAULT_RUBRIC_ID,
                org_id=org_id,
            )
    prev, prev_hash = audit_store.parse_scorecard(row)
    if not refresh and prev_hash == rh and isinstance(prev, dict):
        return prev, rh
    audit = analyze_call(call_id, org_id)
    if isinstance(prev, dict) and prev.get("manual_review"):
        audit["manual_review"] = True
        audit["flagged"] = True
        if prev.get("manual_review_at"):
            audit["manual_review_at"] = prev["manual_review_at"]
    if isinstance(prev, dict) and prev.get("review_solved"):
        audit["flagged"] = True
        audit["review_solved"] = True
        if prev.get("review_solved_at"):
            audit["review_solved_at"] = prev["review_solved_at"]
    with _db_lock:
        with _conn() as c:
            audit_store.upsert_audit(
                c,
                call_id=call_id,
                findings=audit,
                engine_version=rh,
                org_id=org_id,
            )
    return audit, rh


_FLAG_REASON_LABELS = {
    "hostile_language_override": "hostile language",
    "low_overall_score": "low overall score",
}


def _audit_is_flagged(audit) -> bool:
    if not isinstance(audit, dict):
        return False
    return bool(
        audit.get("flagged")
        or audit.get("manual_review")
        or (audit.get("manager_review") or [])
        or (audit.get("gate_fails") or [])
    )


def _flag_sources(audit: dict) -> list[str]:
    sources = []
    if audit.get("manual_review"):
        sources.append("manual")
    auto = bool(audit.get("manager_review") or audit.get("gate_fails"))
    if not auto and audit.get("flagged") and not audit.get("manual_review"):
        auto = True
    if auto:
        sources.append("auto")
    return sources


def _flag_reason_text(audit: dict) -> str:
    parts = []
    if audit.get("manual_review"):
        parts.append("manual flag")
    for t in audit.get("manager_review") or []:
        reason = t.get("reason") if isinstance(t, dict) else t
        label = _FLAG_REASON_LABELS.get(reason, str(reason or "").replace("_", " "))
        if label:
            parts.append(label)
    for g in audit.get("gate_fails") or []:
        if not isinstance(g, str):
            continue
        label = _FLAG_REASON_LABELS.get(g, g.replace("_", " "))
        if label:
            parts.append(label)
    seen = []
    for p in parts:
        if p and p not in seen:
            seen.append(p)
    return "; ".join(seen)


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/api/pyai/status")
def pyai_status(request: Request):
    """
    Safe PyAI key posture + local CallProof usage counters for the UI.
    Never returns the API key. Usage is CallProof-recorded outbound hits
    (PyAI does not publish a remaining-request counter).
    """
    def _snapshot():
        usage = pyai_usage.usage_summary(org_id=_org(request))
        pyai_u = (usage.get("by_provider") or {}).get("pyai") or {}
        claude_u = (usage.get("by_provider") or {}).get("anthropic") or {}
        return usage, {
            "pyai_hits": int(pyai_u.get("hits") or 0),
            "pyai_actions": int(pyai_u.get("actions") or 0),
            "pyai_polls": int(pyai_u.get("polls") or 0),
            "pyai_units": float(pyai_u.get("units") or 0),
            "claude_hits": int(claude_u.get("hits") or 0),
        }

    def _chip_text(stats, balance_label=None):
        bits = [f"{stats['pyai_actions']} PyAI"]
        if stats["pyai_polls"]:
            bits.append(f"{stats['pyai_polls']} polls")
        bits.append(f"{stats['claude_hits']} Claude")
        if stats["pyai_units"]:
            bits.append(f"{stats['pyai_units']:g} units")
        if balance_label:
            bits.append(balance_label)
        return " · ".join(bits)

    def _pack(usage, stats, **extra):
        parts = []
        if stats["pyai_hits"]:
            parts.append(
                f"PyAI {stats['pyai_actions']} calls / {stats['pyai_hits']} hits"
            )
        else:
            parts.append("PyAI 0 hits today")
        if stats["claude_hits"]:
            parts.append(f"Claude {stats['claude_hits']}")
        if stats["pyai_units"]:
            parts.append(f"{stats['pyai_units']:g} units")
        spend = cost_estimate.today_from_usage(usage)
        pyai = (transcribe.PYAI_API_KEY or os.environ.get("PYAI_API_KEY") or "").strip()
        claude = (qa.ANTHROPIC_API_KEY or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        out = {
            "usage": usage,
            "usage_label": " · ".join(parts),
            "cost_today": spend,
            "cost_label": spend.get("label"),
            "pyai_suffix": env_keys.key_suffix(pyai),
            "claude_configured": bool(claude),
            "claude_suffix": env_keys.key_suffix(claude),
            **stats,
        }
        out.update(extra)
        return out

    key = (transcribe.PYAI_API_KEY or os.environ.get("PYAI_API_KEY") or "").strip()
    if not key:
        usage, stats = _snapshot()
        return _pack(
            usage, stats,
            ok=False,
            configured=False,
            env=None,
            label="No key",
            status="missing",
            quota_label=_chip_text(stats, "Add PYAI_API_KEY"),
            healthy=False,
        )

    kind = "sandbox" if key.startswith("pyai_test_") else "live"
    cached = _pyai_me_cached(key)
    r_status = 0
    body: dict = {}
    if cached:
        r_status, body = cached
    else:
        try:
            r = pyai_usage.get(
                f"{transcribe.BASE_URL}/v1/me",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15.0,
            )
        except httpx.HTTPError as e:
            log.warning("pyai /v1/me failed: %s", e)
            usage, stats = _snapshot()
            return _pack(
                usage, stats,
                ok=False,
                configured=True,
                env="test" if kind == "sandbox" else "live",
                label="Sandbox" if kind == "sandbox" else "Live",
                status="unreachable",
                quota_label=_chip_text(stats, "Could not reach PyAI"),
                healthy=False,
                error="unreachable",
            )
        r_status = r.status_code
        if r_status == 429:
            stale = _pyai_me_cached(key, allow_stale=True)
            usage, stats = _snapshot()
            if stale:
                r_status, body = stale
            else:
                return _pack(
                    usage, stats,
                    ok=True,
                    configured=True,
                    env="test" if kind == "sandbox" else "live",
                    label="Sandbox" if kind == "sandbox" else "Live",
                    status="rate_limited",
                    quota_label=_chip_text(stats, "PyAI rate limit — retry shortly"),
                    healthy=True,
                    error="rate_limited",
                )
        elif r_status == 200:
            body = r.json() if r.content else {}
            if isinstance(body, dict):
                _pyai_me_store(key, 200, body)
            else:
                body = {}
        else:
            usage, stats = _snapshot()
            if r_status == 401:
                return _pack(
                    usage, stats,
                    ok=False,
                    configured=True,
                    env="test" if kind == "sandbox" else "live",
                    label="Sandbox" if kind == "sandbox" else "Live",
                    status="unauthorized",
                    quota_label=_chip_text(stats, "Key invalid or revoked"),
                    healthy=False,
                    error="unauthorized",
                )
            return _pack(
                usage, stats,
                ok=False,
                configured=True,
                env="test" if kind == "sandbox" else "live",
                label="Sandbox" if kind == "sandbox" else "Live",
                status="error",
                quota_label=_chip_text(stats, f"HTTP {r_status}"),
                healthy=False,
                error=f"http_{r_status}",
            )

    usage, stats = _snapshot()

    if r_status == 401:
        return _pack(
            usage, stats,
            ok=False,
            configured=True,
            env="test" if kind == "sandbox" else "live",
            label="Sandbox" if kind == "sandbox" else "Live",
            status="unauthorized",
            quota_label=_chip_text(stats, "Key invalid or revoked"),
            healthy=False,
            error="unauthorized",
        )

    if r_status != 200:
        return _pack(
            usage, stats,
            ok=False,
            configured=True,
            env="test" if kind == "sandbox" else "live",
            label="Sandbox" if kind == "sandbox" else "Live",
            status="error",
            quota_label=_chip_text(stats, f"HTTP {r_status}"),
            healthy=False,
            error=f"http_{r_status}",
        )

    env = (body.get("env") or ("test" if kind == "sandbox" else "live")).lower()
    is_sandbox = env == "test" or kind == "sandbox"
    label = "Sandbox" if is_sandbox else "Live"
    limits = body.get("limits") or {}
    key_status = body.get("status") or "unknown"
    healthy = key_status == "active" and (body.get("org_status") or "active") == "active"

    daily_cap = limits.get("daily_unit_cap")
    if is_sandbox:
        # Sandbox is not billed; optional daily unit cap only (not prepaid $).
        balance_label = (
            f"cap {daily_cap} u/day" if daily_cap is not None else "not billed"
        )
        quota_kind = "sandbox_daily"
        quota_value = daily_cap
    else:
        # Live: show local usage only — do not surface prepaid credit balance.
        balance_label = None
        quota_kind = "live_usage"
        quota_value = None

    return _pack(
        usage, stats,
        ok=True,
        configured=True,
        env=env,
        label=label,
        status=key_status,
        org_status=body.get("org_status"),
        plan=body.get("plan"),
        healthy=healthy,
        quota_kind=quota_kind,
        quota_label=_chip_text(stats, balance_label),
        quota_value=quota_value,
        balance_label=balance_label,
        limits={
            "rps": limits.get("rps"),
            "burst": limits.get("burst"),
            "concurrency": limits.get("concurrency"),
            "daily_unit_cap": daily_cap,
            "monthly_units": limits.get("monthly_units"),
        },
    )


class KeyUpdate(BaseModel):
    pyai_api_key: str | None = None
    anthropic_api_key: str | None = None
    justcall_api_key: str | None = None
    justcall_api_secret: str | None = None


class JustCallCredentialBody(BaseModel):
    api_key: str | None = None
    api_secret: str | None = None
    justcall_api_key: str | None = None
    justcall_api_secret: str | None = None


def _require_loopback(request: Request) -> None:
    host = (request.client.host if request.client else "") or ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    # Starlette TestClient reports host=testclient.
    if host == "testclient" and skip_startup():
        return
    raise HTTPException(
        status_code=403,
        detail="Key updates are only allowed from this machine.",
    )


@app.post("/api/keys")
def update_keys(body: KeyUpdate, request: Request):
    """App-owned keys are host env only. JustCall is per-org Vault (integrations)."""
    _require_loopback(request)
    pyai_raw = (body.pyai_api_key or "").strip()
    claude_raw = (body.anthropic_api_key or "").strip()
    if pyai_raw or claude_raw:
        raise HTTPException(
            status_code=400,
            detail=(
                "PyAI and Anthropic keys are host environment variables "
                "(PYAI_API_KEY, ANTHROPIC_API_KEY). They cannot be set from the API."
            ),
        )
    if (body.justcall_api_key or "").strip() or (body.justcall_api_secret or "").strip():
        raise HTTPException(
            status_code=400,
            detail="JustCall credentials are per-organization. Use POST /api/integrations/justcall.",
        )
    raise HTTPException(
        status_code=400,
        detail="Set app keys on the host, or JustCall via Integrations.",
    )


@app.get("/api/dev/logs")
def dev_logs(request: Request, lines: int = 200):
    """
    Tail CallProof's rotating app log (logs/callproof.log) for the Dev Logs UI.
    Secrets are redacted. Same structured events as the terminal callproof.* stream.
    """
    payload = applog.read_tail(lines=lines)
    usage = pyai_usage.usage_summary(org_id=_org(request))
    payload["usage"] = {
        "total_hits": usage.get("total_hits"),
        "total_actions": usage.get("total_actions"),
        "total_polls": usage.get("total_polls"),
        "total_units": usage.get("total_units"),
        "by_provider": usage.get("by_provider"),
        "top_paths": usage.get("top_paths"),
        "window": usage.get("window"),
    }
    return payload


@app.get("/api/calls")
def list_calls(request: Request, source: str | None = None):
    """
    Library listing of stored calls. Omits raw transcript / raw_json
    payloads — those stay on the audit/detail paths.
    """
    org_id = _org(request)
    rh = _rubric_hash()
    source_filter = (source or "").strip().lower() or None
    if source_filter and not re.fullmatch(r"[a-z0-9_]{1,32}", source_filter):
        raise HTTPException(status_code=400, detail="Invalid source filter.")
    # latest-per-rubric (default legacy v8)
    sql = f"""
            SELECT
              c.id,
              c.status,
              c.audio_seconds,
              c.speakers,
              c.created_at,
              c.pyai_call_id,
              c.job_id,
              c.filename,
              c.source,
              c.external_id,
              (SELECT COUNT(*) FROM segments s
                 WHERE s.call_id = c.id AND s.org_id = c.org_id) AS segment_count,
              a.findings,
              a.engine_version,
              a.created_at AS audited_at
            FROM calls c
            {audit_store.latest_default_join_sql()}
            WHERE c.org_id = %s
    """
    params: list = list(audit_store.latest_default_join_params(org_id))
    params.append(org_id)
    if source_filter:
        sql += " AND LOWER(COALESCE(c.source, '')) = %s "
        params.append(source_filter)
    sql += " ORDER BY c.id DESC "
    with _conn() as c:
        rows = c.execute(sql, params).fetchall()

    out = []
    for r in rows:
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        item = {
            "id": r["id"],
            "filename": fname,
            "status": r["status"],
            "audio_seconds": r["audio_seconds"],
            "speakers": r["speakers"],
            "created_at": r["created_at"],
            "pyai_call_id": r["pyai_call_id"],
            "job_id": r["job_id"],
            "segment_count": r["segment_count"] or 0,
            "has_audit": False,
            "audit_fresh": False,
            "score": None,
            "grade": None,
            "flagged": False,
            "review_solved": False,
            "churn_risk": "",
            "audited_at": r["audited_at"],
            "source": (r["source"] or "").strip() or "upload",
            "external_id": r["external_id"],
        }
        if r["findings"]:
            try:
                cached = audit_store.decode_findings(r["findings"])
                if cached is None:
                    item["has_audit"] = True
                else:
                    item["has_audit"] = True
                    item["audit_fresh"] = r["engine_version"] == rh
                    item["score"] = cached.get("score")
                    item["grade"] = cached.get("grade")
                    item["flagged"] = _audit_is_flagged(cached)
                    item["review_solved"] = bool(cached.get("review_solved"))
                    churn = cached.get("churn") or {}
                    if isinstance(churn, dict):
                        item["churn_risk"] = str(churn.get("risk") or "").strip().lower()
            except (TypeError, json.JSONDecodeError):
                item["has_audit"] = True
        item["cost"] = cost_estimate.estimate_call_cost(
            item.get("audio_seconds"),
            has_audit=bool(item.get("has_audit")),
        )
        out.append(item)
    return out


def _temp_audio_path(suffix: str = "") -> str:
    fd, path = tempfile.mkstemp(prefix="callproof_", suffix=suffix)
    os.close(fd)
    return path


def _store_playback(src_path: str, call_id: int, org_id: str) -> None:
    """Copy the recording into the private per-org Storage object."""
    audio_store.put_file(org_id, call_id, src_path)


def _clear_playback_audio(org_id: str, call_ids: list[int] | None = None) -> int:
    """Delete this org's Storage objects. If call_ids is set, only those keys."""
    try:
        if call_ids is not None:
            return audio_store.remove_objects(org_id, call_ids)
        return audio_store.remove_org_prefix(org_id)
    except audio_store.AudioStoreError as e:
        log.warning("could not clear playback audio: %s", e)
        return 0


@app.post("/api/cache/clear")
def clear_cache(request: Request):
    """Delete this org's transcripts, scorecards, and playback audio."""
    org_id = _org(request)
    with _db_lock:
        with _conn() as c:
            id_rows = c.execute(
                "SELECT id FROM calls WHERE org_id = %s", (org_id,),
            ).fetchall()
            call_ids = [int(r["id"]) for r in id_rows]
            n_calls = c.execute(
                "SELECT COUNT(*) AS n FROM calls WHERE org_id = %s", (org_id,),
            ).fetchone()["n"]
            n_segments = c.execute(
                "SELECT COUNT(*) AS n FROM segments WHERE org_id = %s", (org_id,),
            ).fetchone()["n"]
            n_audits = c.execute(
                "SELECT COUNT(*) AS n FROM audits WHERE org_id = %s", (org_id,),
            ).fetchone()["n"]
            c.execute("DELETE FROM audits WHERE org_id = %s", (org_id,))
            c.execute("DELETE FROM segments WHERE org_id = %s", (org_id,))
            c.execute("DELETE FROM calls WHERE org_id = %s", (org_id,))
    n_audio = _clear_playback_audio(org_id, call_ids)
    applog.event(
        log, "cache_cleared",
        calls=n_calls, segments=n_segments, audits=n_audits, audio=n_audio,
    )
    log.info(
        "cache cleared: %d call(s), %d segment(s), %d scorecard(s), %d audio item(s)",
        n_calls, n_segments, n_audits, n_audio,
    )
    return {
        "status": "ok",
        "deleted": {
            "calls": n_calls,
            "segments": n_segments,
            "audits": n_audits,
            "audio": n_audio,
        },
    }


def _recap_export_fields(recap: dict | None) -> tuple[str, str, str]:
    recap = recap or {}
    if recap.get("status") and recap.get("status") != "ok":
        err = (recap.get("error") or recap.get("status") or "").strip()
        return "", "", err
    tldr = (recap.get("tldr") or recap.get("headline") or "").strip()
    summary = (recap.get("summary") or "").strip()
    actions = []
    for it in recap.get("action_items") or []:
        if isinstance(it, dict):
            task = (it.get("task") or "").strip()
            meta = " · ".join(
                x for x in [(it.get("owner") or "").strip(), (it.get("due") or "").strip()] if x
            )
            if task:
                actions.append(f"{task} ({meta})" if meta else task)
        elif it:
            actions.append(str(it).strip())
    return tldr, summary, " | ".join(actions)


def _ratings_export_text(findings: list | None) -> str:
    parts = []
    for f in findings or []:
        name = f.get("name") or f.get("id") or "criterion"
        verdict = f.get("verdict") or ""
        pts = f.get("points")
        weight = f.get("weight")
        if pts is not None and weight is not None:
            parts.append(f"{name}={verdict} ({pts}/{weight})")
        else:
            parts.append(f"{name}={verdict}")
    return " | ".join(parts)


_SCORECARD_DIM_ORDER = (
    "resolution_effectiveness",
    "ownership_next_steps",
    "active_listening",
    "tone_empathy_professionalism",
)
_AGENT_NAME_RE = re.compile(
    r"\b(?:my name is|i am|i'm)\s+([a-z][a-z'`.-]{1,32}(?:\s+[a-z][a-z'`.-]{1,32})?)",
    re.I,
)
_AGENT_NAME_STOP = {
    "calling", "from", "with", "your", "the", "here", "today", "a", "an",
    "sorry", "happy", "glad", "going", "looking", "checking", "trying",
    "just", "so", "very", "really",
}


def _xml_text(value) -> str:
    return xml_escape("" if value is None else str(value), {'"': "&quot;"})


def _agent_display_name(audit: dict) -> str:
    """Best-effort name from the agent's opening turns; else speaker label."""
    speaker = audit.get("agent_speaker")
    snippets = []
    for s in audit.get("segments") or []:
        if speaker and s.get("speaker") != speaker:
            continue
        snippets.append(s.get("text") or "")
        if len(snippets) >= 12:
            break
    blob = " ".join(snippets)
    m = _AGENT_NAME_RE.search(blob)
    if m:
        words = [
            w for w in m.group(1).replace(".", " ").split()
            if w.lower() not in _AGENT_NAME_STOP
        ]
        if words:
            return " ".join(words).title()
    return speaker or "Unknown"


def _score_style_id(score) -> str:
    try:
        n = float(score)
    except (TypeError, ValueError):
        return "cell"
    if n >= 80:
        return "scoreGood"
    if n >= 60:
        return "scoreMid"
    return "scoreLow"


def _scorecard_dimension_columns(records: list[dict]) -> list[tuple[str, str]]:
    """Return [(finding_id, header_label), ...] in rubric order, then extras."""
    seen: dict[str, str] = {}
    for rec in records:
        for f in rec.get("findings") or []:
            fid = f.get("id") or f.get("name")
            if not fid:
                continue
            seen.setdefault(str(fid), f.get("name") or str(fid))
    ordered = []
    for fid in _SCORECARD_DIM_ORDER:
        if fid in seen:
            ordered.append((fid, seen.pop(fid)))
    rest = sorted(seen.items(), key=lambda kv: kv[1].lower())
    return ordered + rest


def _scorecard_cell_xml(value, style="cell", number=False) -> str:
    if value is None or value == "":
        return f'<Cell ss:StyleID="{style}"/>'
    if number:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return (
                f'<Cell ss:StyleID="{style}">'
                f'<Data ss:Type="String">{_xml_text(value)}</Data></Cell>'
            )
        if num.is_integer():
            shown = str(int(num))
        else:
            shown = str(num)
        return (
            f'<Cell ss:StyleID="{style}">'
            f'<Data ss:Type="Number">{shown}</Data></Cell>'
        )
    return (
        f'<Cell ss:StyleID="{style}">'
        f'<Data ss:Type="String">{_xml_text(value)}</Data></Cell>'
    )


def _scorecard_xls(records: list[dict]) -> bytes:
    """Excel XML Spreadsheet (opens in Excel/Sheets) with a colored Score column.

    Plain CSV cannot store cell colors; this is the spreadsheet form of a CSV table.
    """
    dim_cols = _scorecard_dimension_columns(records)
    headers = [
        "Recording ID",
        "Recording name",
        "Agent name",
        "Score",
        "Grade",
    ] + [label for _fid, label in dim_cols]

    rows_xml = [
        "<Row>"
        + "".join(_scorecard_cell_xml(h, style="header") for h in headers)
        + "</Row>"
    ]
    for rec in records:
        by_id = {}
        for f in rec.get("findings") or []:
            fid = f.get("id") or f.get("name")
            if fid:
                by_id[str(fid)] = f
        score = rec.get("score")
        cells = [
            _scorecard_cell_xml(rec.get("call_id"), number=True),
            _scorecard_cell_xml(rec.get("filename")),
            _scorecard_cell_xml(rec.get("agent_name")),
            _scorecard_cell_xml(score, style=_score_style_id(score), number=True),
            _scorecard_cell_xml(rec.get("grade")),
        ]
        for fid, _label in dim_cols:
            f = by_id.get(fid) or {}
            pts = f.get("points")
            weight = f.get("weight")
            if pts is not None and weight is not None:
                cells.append(_scorecard_cell_xml(f"{pts}/{weight}"))
            elif pts is not None:
                cells.append(_scorecard_cell_xml(pts, number=True))
            else:
                cells.append(_scorecard_cell_xml(f.get("verdict")))
        rows_xml.append("<Row>" + "".join(cells) + "</Row>")

    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:o="urn:schemas-microsoft-com:office:office"
 xmlns:x="urn:schemas-microsoft-com:office:excel"
 xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">
 <Styles>
  <Style ss:ID="header">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#1F2937" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="cell"/>
  <Style ss:ID="scoreGood">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#16A34A" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="scoreMid">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#EA580C" ss:Pattern="Solid"/>
  </Style>
  <Style ss:ID="scoreLow">
   <Font ss:Bold="1" ss:Color="#FFFFFF"/>
   <Interior ss:Color="#DC2626" ss:Pattern="Solid"/>
  </Style>
 </Styles>
 <Worksheet ss:Name="Scorecard">
  <Table>
   {"".join(rows_xml)}
  </Table>
 </Worksheet>
</Workbook>
"""
    return xml.encode("utf-8")


def _load_scorecard_records(org_id: str) -> list[dict]:
    # latest-per-rubric (default legacy v8)
    join_sql = audit_store.latest_default_join_sql(inner=True)
    join_params = audit_store.latest_default_join_params(org_id)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              c.id,
              c.filename,
              a.findings
            FROM calls c
            {join_sql}
            WHERE c.org_id = %s
              AND (c.status = 'completed' OR c.status IS NULL OR c.status = '')
            ORDER BY c.id ASC
            """,
            (*join_params, org_id),
        ).fetchall()
    records = []
    for r in rows:
        audit = audit_store.decode_findings(r["findings"])
        if not isinstance(audit, dict):
            continue
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        records.append({
            "call_id": r["id"],
            "filename": fname,
            "agent_name": _agent_display_name(audit),
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "findings": audit.get("findings") or [],
        })
    return records


@app.get("/api/calls/flagged")
def list_flagged_calls(request: Request):
    """Scorecards flagged for manager review (manual button or auto triggers)."""
    org_id = _org(request)
    # latest-per-rubric (default legacy v8)
    join_sql = audit_store.latest_default_join_sql(inner=True)
    join_params = audit_store.latest_default_join_params(org_id)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              c.id,
              c.filename,
              c.audio_seconds,
              c.created_at,
              a.findings,
              a.created_at AS audited_at
            FROM calls c
            {join_sql}
            WHERE c.org_id = %s
            ORDER BY c.id DESC
            """,
            (*join_params, org_id),
        ).fetchall()
    out = []
    for r in rows:
        audit = audit_store.decode_findings(r["findings"])
        if not isinstance(audit, dict):
            continue
        if not _audit_is_flagged(audit):
            continue
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        recap = audit.get("recap") if isinstance(audit.get("recap"), dict) else {}
        out.append({
            "id": r["id"],
            "filename": fname,
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "agent_name": _agent_display_name(audit),
            "audio_seconds": r["audio_seconds"],
            "flagged": True,
            "manual_review": bool(audit.get("manual_review")),
            "solved": bool(audit.get("review_solved")),
            "sources": _flag_sources(audit),
            "reasons": _flag_reason_text(audit),
            "recap_tldr": (recap.get("tldr") or recap.get("headline") or "").strip(),
            "created_at": r["created_at"],
            "audited_at": r["audited_at"],
            "manual_review_at": audit.get("manual_review_at"),
            "review_solved_at": audit.get("review_solved_at"),
        })
    pending = sum(1 for i in out if not i["solved"])
    applog.event(log, "flagged_list", count=len(out), pending=pending, solved=len(out) - pending)
    return out


@app.get("/api/calls/export-scorecard")
def export_scorecard(request: Request):
    """Scorecard spreadsheet: recording id/name, agent, overall score (colored),
    and each scoring area. Excel XML so the Score column can be green/orange/red
    (plain CSV cannot store cell colors)."""
    records = _load_scorecard_records(_org(request))
    if not records:
        raise HTTPException(
            status_code=404,
            detail="No audited calls to export. Score a call first.",
        )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    body = _scorecard_xls(records)
    applog.event(log, "scorecard_exported", count=len(records))
    log.info("scorecard export %d audited call(s)", len(records))
    return Response(
        content=body,
        media_type="application/vnd.ms-excel",
        headers={
            "Content-Disposition": (
                f'attachment; filename="callproof-scorecard-{stamp}.xls"'
            ),
        },
    )


@app.get("/api/calls/export")
def export_calls(request: Request, format: str = "csv"):
    """
    One-click bulk export of score/grade, finding ratings, and recap.
    Omits raw transcripts. Defaults to CSV download; use format=json for JSON.
    """
    org_id = _org(request)
    fmt = (format or "csv").strip().lower()
    if fmt not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")

    # latest-per-rubric (default legacy v8)
    join_params = audit_store.latest_default_join_params(org_id)
    with _conn() as c:
        rows = c.execute(
            f"""
            SELECT
              c.id,
              c.filename,
              c.status,
              c.audio_seconds,
              c.created_at,
              a.findings,
              a.created_at AS audited_at
            FROM calls c
            {audit_store.latest_default_join_sql(inner=True)}
            WHERE c.org_id = %s
              AND (c.status = 'completed' OR c.status IS NULL OR c.status = '')
            ORDER BY c.id ASC
            """,
            (*join_params, org_id),
        ).fetchall()

    records = []
    for r in rows:
        audit = audit_store.decode_findings(r["findings"])
        if not isinstance(audit, dict):
            continue

        recap_tldr, recap_summary, recap_actions = _recap_export_fields(audit.get("recap"))
        churn = audit.get("churn") or {}
        fname = (r["filename"] or "").strip() or f"call-{r['id']}.mp3"
        records.append({
            "call_id": r["id"],
            "filename": fname,
            "created_at": r["created_at"] or "",
            "audited_at": r["audited_at"] or "",
            "audio_seconds": r["audio_seconds"],
            "score": audit.get("score"),
            "grade": audit.get("grade") or "",
            "flagged": bool(audit.get("flagged")),
            "churn_risk": (churn.get("risk") or ""),
            "ratings": _ratings_export_text(audit.get("findings")),
            "recap_tldr": recap_tldr,
            "recap_summary": recap_summary,
            "recap_actions": recap_actions,
        })

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    applog.event(log, "calls_exported", count=len(records), format=fmt)
    log.info("bulk export %d audited call(s) as %s", len(records), fmt)

    if fmt == "json":
        body = json.dumps({"exported_at": stamp, "count": len(records), "calls": records}, indent=2)
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="callproof-export-{stamp}.json"',
            },
        )

    buf = io.StringIO()
    fields = [
        "filename", "call_id", "created_at", "audited_at", "audio_seconds",
        "score", "grade", "flagged", "churn_risk", "ratings",
        "recap_tldr", "recap_summary", "recap_actions",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for rec in records:
        writer.writerow(rec)

    # UTF-8 BOM helps Excel open the CSV cleanly
    content = "\ufeff" + buf.getvalue()
    return Response(
        content=content,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="callproof-export-{stamp}.csv"',
        },
    )


def _hydrate_audit_segments(audit: dict, call_id: int, org_id: str) -> dict:
    """Serve live expanded turns, not the frozen Hear blob stored in the scorecard."""
    if not isinstance(audit, dict) or call_id < 1:
        return audit
    try:
        with _conn() as c:
            rows = c.execute(
                """
                SELECT seq, speaker, channel, "start", "end", text
                FROM segments WHERE call_id = %s AND org_id = %s ORDER BY seq
                """,
                (call_id, org_id),
            ).fetchall()
    except Exception:
        return audit
    segs = transcribe.expand_tagged_segments([dict(r) for r in rows])
    out = dict(audit)
    out["segments"] = segs
    agent = out.get("agent_speaker")
    if agent is None or (isinstance(agent, str) and not str(agent).strip()):
        out["agent_speaker"] = qa.classify_roles(segs)
    return out


@app.get("/api/calls/{call_id}/audit")
def get_audit(call_id: int, request: Request, refresh: bool = False):
    org_id = _org(request)
    rh = _rubric_hash()
    if not refresh:
        with _conn() as c:
            # latest-per-rubric (default legacy v8)
            row = audit_store.fetch_latest_for_rubric(
                c,
                call_id=call_id,
                rubric_id=DEFAULT_RUBRIC_ID,
                org_id=org_id,
            )
        cached, cached_hash = audit_store.parse_scorecard(row)
        if cached and cached_hash == rh:
            applog.event(
                log, "audit_cache",
                result="HIT", call_id=call_id, score=cached.get("score"),
            )
            log.info(
                "cache HIT  call %d (score %s) - returning stored audit",
                call_id, cached.get("score"),
            )
            return _attach_filename(
                _hydrate_audit_segments(cached, call_id, org_id), call_id, org_id,
            )
        applog.event(log, "audit_cache", result="MISS", call_id=call_id)
        log.info("cache MISS  call %d - computing fresh audit", call_id)
    else:
        applog.event(log, "audit_cache", result="BYPASS", call_id=call_id)
        log.info("cache BYPASS (refresh) call %d - computing fresh audit", call_id)
    audit, _rh = _load_or_compute_audit(call_id, org_id, refresh=True)
    log.info("cached audit for call %d (score %s)", call_id, audit["score"])
    return _attach_filename(
        _hydrate_audit_segments(audit, call_id, org_id), call_id, org_id,
    )


def _save_audit(call_id: int, audit: dict, rh: str, org_id: str):
    with _conn() as c:
        audit_store.upsert_audit(
            c,
            call_id=call_id,
            findings=audit,
            engine_version=rh,
            org_id=org_id,
        )


@app.post("/api/calls/{call_id}/flag")
def flag_call_for_review(call_id: int, request: Request):
    """Persist a manual manager-review flag on the stored scorecard."""
    org_id = _org(request)
    if call_id < 1:
        raise HTTPException(status_code=400, detail="Invalid call id.")
    with _conn() as c:
        # latest-per-rubric (default legacy v8)
        row = audit_store.fetch_latest_for_rubric(
            c,
            call_id=call_id,
            rubric_id=DEFAULT_RUBRIC_ID,
            org_id=org_id,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="No scorecard for this call. Score it before flagging.",
        )
    audit, stored_hash = audit_store.parse_scorecard(row)
    if not isinstance(audit, dict):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    already = _audit_is_flagged(audit)
    already_manual = bool(audit.get("manual_review"))
    already_solved = bool(audit.get("review_solved"))
    audit["flagged"] = True
    audit["manual_review"] = True
    if not already:
        audit["review_solved"] = False
        audit["review_solved_at"] = None
    if not audit.get("manual_review_at"):
        audit["manual_review_at"] = datetime.now(timezone.utc).isoformat()
    rh = stored_hash or _rubric_hash()
    _save_audit(call_id, audit, rh, org_id)
    applog.event(
        log, "call_flagged",
        call_id=call_id,
        source="manual",
        already=already_manual,
        solved=bool(audit.get("review_solved")),
    )
    log.info("call %d flagged for manual review", call_id)
    return {
        "status": "ok",
        "call_id": call_id,
        "flagged": True,
        "manual_review": True,
        "solved": bool(audit.get("review_solved")),
        "reasons": _flag_reason_text(audit),
        "already": already_manual or already_solved,
    }


@app.post("/api/calls/{call_id}/solve")
def solve_flagged_review(call_id: int, request: Request):
    """Move a flagged scorecard from Pending to Solved."""
    org_id = _org(request)
    if call_id < 1:
        raise HTTPException(status_code=400, detail="Invalid call id.")
    with _conn() as c:
        # latest-per-rubric (default legacy v8)
        row = audit_store.fetch_latest_for_rubric(
            c,
            call_id=call_id,
            rubric_id=DEFAULT_RUBRIC_ID,
            org_id=org_id,
        )
    if not row:
        raise HTTPException(
            status_code=404,
            detail="No scorecard for this call.",
        )
    audit, stored_hash = audit_store.parse_scorecard(row)
    if not isinstance(audit, dict):
        raise HTTPException(status_code=500, detail="Stored scorecard is not valid JSON.")
    if not _audit_is_flagged(audit):
        raise HTTPException(
            status_code=400,
            detail="This call is not in the review queue.",
        )
    already = bool(audit.get("review_solved"))
    audit["flagged"] = True
    audit["review_solved"] = True
    if not audit.get("review_solved_at"):
        audit["review_solved_at"] = datetime.now(timezone.utc).isoformat()
    rh = stored_hash or _rubric_hash()
    _save_audit(call_id, audit, rh, org_id)
    applog.event(
        log, "review_solved",
        call_id=call_id,
        already=already,
    )
    log.info("call %d review marked solved", call_id)
    return {
        "status": "ok",
        "call_id": call_id,
        "flagged": True,
        "solved": True,
        "already": already,
    }


def _ensure_retention_draft(call_id: int, audit: dict, rh: str, org_id: str) -> dict:
    """
    Run the retention Claude draft once if missing, cache on the audit, return updated audit.
    """
    existing = audit.get("retention_email") or {}
    if existing.get("status") == "ok" and (existing.get("body") or "").strip():
        return audit

    call_id, _meta, segments = qa.load_call(call_id, org_id=org_id)
    if not segments:
        audit["retention_email"] = {
            "status": "error",
            "error": "No transcript segments available for retention draft.",
            "subject": "",
            "body": "",
            "summary": "",
            "suggested_actions": [],
        }
        _save_audit(call_id, audit, rh, org_id)
        return audit

    agent = audit.get("agent_speaker") or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    log.info("on-demand retention draft for call %d", call_id)
    draft = qa.draft_retention_email(transcript_text, segments)
    audit["retention_email"] = draft
    _save_audit(call_id, audit, rh, org_id)
    return audit


@app.post("/api/calls/{call_id}/feedback")
def post_feedback(call_id: int, request: Request):
    """On-demand areas of improvement (Sonnet, effort=high). Cached after first success
    that includes at least one agent insight."""
    org_id = _org(request)
    audit, rh = _load_or_compute_audit(call_id, org_id, refresh=False)
    existing = audit.get("feedback") or {}
    if existing.get("status") == "ok" and (existing.get("agent") or []):
        log.info("on-demand feedback cache HIT for call %d", call_id)
        applog.event(log, "feedback_cache", result="HIT", call_id=call_id)
        return {"call_id": call_id, "feedback": existing}

    _cid, _meta, segments = qa.load_call(call_id, org_id=org_id)
    if not segments:
        audit["feedback"] = {
            "status": "error",
            "error": "No transcript segments available for areas of improvement.",
            "agent": [],
            "product": [],
        }
        _save_audit(call_id, audit, rh, org_id)
        applog.event(
            log, "feedback_failure", level=logging.ERROR,
            call_id=call_id, error="no_segments",
        )
        return {"call_id": call_id, "feedback": audit["feedback"]}

    agent = audit.get("agent_speaker") or qa.classify_roles(segments)
    transcript_text = qa.format_transcript(segments, agent)
    log.info(
        "on-demand areas of improvement for call %d (model=%s effort=%s)",
        call_id, qa.MODEL, qa.CLAUDE_EFFORT,
    )
    feedback = qa.extract_feedback(
        transcript_text, segments, findings=audit.get("findings"),
    )
    audit["feedback"] = feedback
    _save_audit(call_id, audit, rh, org_id)
    applog.event(
        log, "feedback_success" if feedback.get("status") == "ok" else "feedback_failure",
        call_id=call_id,
        agent_items=len(feedback.get("agent") or []),
        product_items=len(feedback.get("product") or []),
        status=feedback.get("status"),
        model="claude-sonnet-5",
        effort="high",
    )
    return {"call_id": call_id, "feedback": feedback}


@app.get("/api/calls/{call_id}/stakeholder-email/compose")
def get_stakeholder_email_compose(call_id: int, request: Request):
    """
    Prefill a Gmail compose draft for this call's churn alert.
    Drafts the retention email with Claude on first use, then caches it.
    Frontend opens gmail_url in a new tab (user sends from their own Gmail).
    """
    org_id = _org(request)
    audit, rh = _load_or_compute_audit(call_id, org_id, refresh=False)
    risk = ((audit.get("churn") or {}).get("risk") or "").lower()
    if risk not in ("high", "medium"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Stakeholder email is only available for medium/high churn risk "
                f"(this call is '{risk or 'unknown'}')."
            ),
        )

    audit = _ensure_retention_draft(call_id, audit, rh, org_id)
    payload = email_notify.build_compose_payload(call_id, audit)
    log.info(
        "stakeholder Gmail compose for call %d (risk=%s, to=%s, retention=%s)",
        call_id, risk, payload.get("to") or "(blank)",
        (audit.get("retention_email") or {}).get("status"),
    )
    return {
        "call_id": call_id,
        "status": "compose",
        "churn_risk": risk,
        "to": payload["to"],
        "subject": payload["subject"],
        "body": payload["body"],
        "gmail_url": payload["gmail_url"],
        "retention_email": audit.get("retention_email"),
    }


def _ingest_audio_file(
    src_path: str,
    source_name: str,
    *,
    org_id: str,
    identity: str | None = None,
    source: str | None = None,
    external_id: str | None = None,
) -> tuple[int, bool]:
    """
    Dedup or transcribe one local audio file. Hear temp is unique per src_path.
    Returns (call_id, deduped). Caller stores the playback copy.
    """
    source_name = transcribe.sanitize_filename(source_name)
    identity = identity or transcribe.identity_for(src_path)
    size = os.path.getsize(src_path)
    hear_tmp = f"{src_path}.{uuid.uuid4().hex}.hear.wav"

    try:
        with _db_lock:
            with db.connection() as conn:
                existing = None
                if source and external_id:
                    existing = transcribe.find_existing_external(
                        conn, source, external_id, org_id=org_id,
                    )
                if not existing:
                    existing = transcribe.find_existing_call(
                        conn, identity, org_id=org_id,
                    )
                if existing:
                    call_id = int(existing["id"])
                    transcribe.set_filename_if_empty(
                        conn, call_id, source_name, org_id=org_id,
                    )
                    applog.event(
                        log, "transcription_success",
                        call_id=call_id, deduped=True, size_bytes=size,
                        filename=source_name,
                    )
                    log.info(
                        "upload deduped to existing call %d (no re-transcription)",
                        call_id,
                    )
                    return call_id, True

        pyai_id = transcribe.new_pyai_call_id()
        job_id, result, mode = transcribe.transcribe_with_fallback(
            src_path, hear_tmp, call_id=pyai_id,
        )
        with _db_lock:
            with db.connection() as conn:
                existing = None
                if source and external_id:
                    existing = transcribe.find_existing_external(
                        conn, source, external_id, org_id=org_id,
                    )
                if not existing:
                    existing = transcribe.find_existing_call(
                        conn, identity, org_id=org_id,
                    )
                if existing:
                    call_id = int(existing["id"])
                    transcribe.set_filename_if_empty(
                        conn, call_id, source_name, org_id=org_id,
                    )
                    return call_id, True
                try:
                    call_id = transcribe.save_transcript(
                        conn, identity, job_id, result,
                        pyai_call_id=pyai_id,
                        filename=source_name,
                        source=source,
                        external_id=external_id,
                        org_id=org_id,
                    )
                except db.IntegrityError:
                    conn.rollback()
                    existing = transcribe.find_existing_call(
                        conn, identity, org_id=org_id,
                    )
                    if not existing:
                        raise
                    call_id = int(existing["id"])
                    transcribe.set_filename_if_empty(
                        conn, call_id, source_name, org_id=org_id,
                    )
                    return call_id, True
        applog.event(
            log, "transcription_success",
            call_id=call_id,
            pyai_call_id=pyai_id,
            job_id=job_id,
            segments=len(result.get("segments") or []),
            size_bytes=size,
            deduped=False,
            filename=source_name,
            mode=mode,
        )
        log.info(
            "transcription complete -> new call %d (filename=%s, pyai_call_id=%s)",
            call_id, source_name, pyai_id,
        )
        return call_id, False
    finally:
        if os.path.exists(hear_tmp):
            os.remove(hear_tmp)


_justcall_inflight: set[str] = set()
_justcall_inflight_lock = threading.Lock()


def _justcall_pair(org_id: str, *, host_fallback: bool = False) -> tuple[str, str] | None:
    """Load this org's Vault pair. Host env only for the operator ingest org."""
    try:
        secret = org_vault.load_justcall(org_id)
    except (org_vault.VaultUnavailable, org_vault.VaultError):
        secret = None
    if secret:
        return secret.api_key, secret.api_secret
    if host_fallback and justcall.host_configured():
        key = (os.environ.get("JUSTCALL_API_KEY") or "").strip()
        sec = (os.environ.get("JUSTCALL_API_SECRET") or "").strip()
        if key and sec:
            return key, sec
    return None


def _justcall_status(org_id: str) -> dict:
    try:
        st = org_vault.status(org_id)
    except Exception:
        st = {"configured": False, "suffix": None}
    return {
        "configured": bool(st.get("configured")),
        "polling": _justcall_poller_started,
        "poll_seconds": justcall.poll_seconds(),
        "key_suffix": st.get("suffix"),
    }


def _process_justcall_call(
    call_id: str,
    payload: dict | None = None,
    *,
    org_id: str,
    host_fallback: bool = False,
) -> dict:
    """Download a JustCall recording, transcribe, score. Safe to retry."""
    cid = str(call_id or "").strip()
    if not cid or "/" in cid or "\\" in cid or ".." in cid:
        raise ValueError("Invalid JustCall call id.")
    with _justcall_inflight_lock:
        if cid in _justcall_inflight:
            return {"status": "in_flight", "justcall_id": cid}
        _justcall_inflight.add(cid)
    tmp = None
    creds_cm = None
    try:
        pair = _justcall_pair(org_id, host_fallback=host_fallback)
        if not pair:
            applog.event(log, "justcall_recording_skip", justcall_id=cid, reason="no_credentials")
            return {"status": "pending_recording", "justcall_id": cid}
        creds_cm = justcall.bound_credentials(pair[0], pair[1])
        creds_cm.__enter__()
        with org_scope(org_id):
            with _db_lock:
                with db.connection() as conn:
                    existing = transcribe.find_existing_external(
                        conn, "justcall", cid, org_id=org_id,
                    )
            if existing:
                local_id = int(existing["id"])
                missing = True
                try:
                    missing = not audio_store.object_exists(org_id, local_id)
                except audio_store.AudioStoreError:
                    missing = True
                if missing:
                    data = justcall.download_recording(cid)
                    if data:
                        suffix = justcall.recording_suffix(data)
                        tmp = _temp_audio_path(suffix)
                        with open(tmp, "wb") as f:
                            f.write(data)
                        _store_playback(tmp, local_id, org_id)
                _load_or_compute_audit(local_id, org_id)
                applog.event(
                    log, "justcall_ingest",
                    justcall_id=cid, call_id=local_id, deduped=True,
                )
                return {"status": "existing", "justcall_id": cid, "call_id": local_id}

            data = justcall.download_recording(cid)
            if not data:
                applog.event(log, "justcall_recording_skip", justcall_id=cid)
                return {"status": "pending_recording", "justcall_id": cid}

            suffix = justcall.recording_suffix(data)
            tmp = _temp_audio_path(suffix)
            with open(tmp, "wb") as f:
                f.write(data)
            source_name = justcall.display_name(payload or {}, cid)
            identity = justcall.identity_for(cid)
            local_id, deduped = _ingest_audio_file(
                tmp, source_name,
                org_id=org_id,
                identity=identity,
                source="justcall",
                external_id=cid,
            )
            _store_playback(tmp, local_id, org_id)
            audit, _rh = _load_or_compute_audit(local_id, org_id)
            applog.event(
                log, "justcall_ingest",
                justcall_id=cid,
                call_id=local_id,
                deduped=deduped,
                score=audit.get("score") if isinstance(audit, dict) else None,
            )
            return {
                "status": "ok",
                "justcall_id": cid,
                "call_id": local_id,
                "deduped": deduped,
                "score": audit.get("score") if isinstance(audit, dict) else None,
            }
    finally:
        if creds_cm is not None:
            creds_cm.__exit__(None, None, None)
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
        with _justcall_inflight_lock:
            _justcall_inflight.discard(cid)


def _sync_justcall_recent(hours: int = 24, *, org_id: str, host_fallback: bool = False) -> dict:
    """Pull recent JustCall calls, ingest any that are not stored yet."""
    pair = _justcall_pair(org_id, host_fallback=host_fallback)
    if not pair:
        raise RuntimeError(
            "JustCall is not connected. Save the API key and secret on the Integrations page."
        )
    with justcall.bound_credentials(pair[0], pair[1]):
        rows = justcall.list_recent_calls(hours=hours)
    queued = 0
    existing = 0
    pending = 0
    errors = 0
    results = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = justcall.extract_completed_id(row)
        if not cid:
            continue
        rec = row.get("call_info", {}).get("recording")
        if rec is None or rec is False or (isinstance(rec, str) and not rec.strip()):
            pending += 1
            results.append({"justcall_id": cid, "status": "pending_recording"})
            continue
        try:
            out = _process_justcall_call(
                cid, row, org_id=org_id, host_fallback=host_fallback,
            )
        except Exception as e:  # noqa: BLE001
            errors += 1
            msg = str(e)[:300]
            applog.event(
                log, "justcall_ingest_failed",
                level=logging.ERROR,
                justcall_id=cid,
                error=msg,
            )
            results.append({"justcall_id": cid, "status": "error"})
            continue
        status = out.get("status")
        if status == "ok":
            queued += 1
        elif status == "existing":
            existing += 1
        elif status == "pending_recording":
            pending += 1
        results.append(out)
    applog.event(
        log, "justcall_sync",
        listed=len(rows),
        ingested=queued,
        existing=existing,
        pending_recording=pending,
        errors=errors,
    )
    return {
        "listed": len(rows),
        "ingested": queued,
        "existing": existing,
        "pending_recording": pending,
        "errors": errors,
        "calls": results,
    }


def _justcall_poll_loop():
    while True:
        try:
            ids = org_vault.list_org_ids()
        except Exception:
            ids = []
        host_oid = integration_org_id()
        if justcall.host_configured() and host_oid not in ids:
            ids = list(ids) + [host_oid]
        for oid in ids:
            try:
                with org_scope(oid):
                    _sync_justcall_recent(
                        org_id=oid,
                        host_fallback=(oid == host_oid),
                    )
            except Exception as e:  # noqa: BLE001
                applog.event(
                    log, "justcall_poll_error",
                    level=logging.ERROR,
                    error=str(e)[:300],
                )
        time.sleep(justcall.poll_seconds())


def _start_justcall_poller():
    global _justcall_poller_started
    if _justcall_poller_started or skip_startup():
        return
    _justcall_poller_started = True
    threading.Thread(
        target=_justcall_poll_loop,
        name="justcall-poller",
        daemon=True,
    ).start()
    applog.event(
        log, "justcall_poller_started",
        interval_seconds=justcall.poll_seconds(),
    )


@app.get("/api/integrations/justcall")
def justcall_integration_status(request: Request):
    return _justcall_status(_org(request))


@app.post("/api/integrations/justcall")
def justcall_save_credentials(body: JustCallCredentialBody, request: Request):
    """Store this org's JustCall pair in Vault. Never echoes the secret."""
    org_id = _org(request)
    key_raw = (body.api_key or body.justcall_api_key or "").strip()
    secret_raw = (body.api_secret or body.justcall_api_secret or "").strip()
    if not key_raw or not secret_raw:
        raise HTTPException(
            status_code=400,
            detail="Paste both the JustCall API key and the API secret.",
        )
    try:
        key = env_keys.normalize_justcall_key(key_raw)
        secret = env_keys.normalize_justcall_secret(secret_raw)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        suffix = org_vault.put_justcall(org_id, key, secret)
    except org_vault.VaultUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Credential vault is not available on this database.",
        ) from None
    except org_vault.VaultError:
        raise HTTPException(
            status_code=502,
            detail="Could not store JustCall credentials.",
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    _start_justcall_poller()
    applog.event(log, "justcall_credentials_saved")
    return {
        "ok": True,
        "configured": True,
        "key_suffix": suffix or None,
    }


@app.delete("/api/integrations/justcall")
def justcall_delete_credentials(request: Request):
    """Remove this org's JustCall pair from Vault. Never echoes the secret."""
    org_id = _org(request)
    try:
        existed = org_vault.delete_justcall(org_id)
    except org_vault.VaultUnavailable:
        existed = False
    except org_vault.VaultError:
        raise HTTPException(
            status_code=502,
            detail="Could not remove JustCall credentials.",
        ) from None
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    applog.event(log, "justcall_credentials_removed")
    return {
        "ok": True,
        "configured": False,
        "removed": existed,
        "key_suffix": None,
    }


@app.post("/api/integrations/justcall/sync")
def justcall_sync_now(request: Request):
    try:
        return _sync_justcall_recent(org_id=_org(request), host_fallback=False)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=502,
            detail="JustCall API request failed.",
        ) from e


@app.post("/api/integrations/justcall/webhook")
async def justcall_webhook(request: Request):
    """
    JustCall call.completed (or URL-validation ping). Returns 200 quickly;
    transcription + scoring run on a background thread.
    """
    raw = await request.body()
    try:
        payload = json.loads(raw.decode("utf-8") or "{}") if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    cid = justcall.extract_completed_id(payload)
    if not cid:
        applog.event(log, "justcall_webhook", accepted=False, reason="validation_or_ignored")
        return {"ok": True, "accepted": False}
    sig = (
        request.headers.get("X-JustCall-Signature")
        or request.headers.get("X-Justcall-Signature")
        or request.headers.get("X-Webhook-Signature")
    )
    if not justcall.verify_webhook_signature(raw, sig):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    org_id = integration_org_id()
    if not _justcall_pair(org_id, host_fallback=True):
        applog.event(log, "justcall_webhook", accepted=False, reason="not_configured")
        raise HTTPException(
            status_code=503,
            detail="JustCall API credentials are not set for this organization.",
        )
    threading.Thread(
        target=_process_justcall_call,
        kwargs={
            "call_id": cid,
            "payload": payload,
            "org_id": org_id,
            "host_fallback": True,
        },
        name=f"justcall-{cid}",
        daemon=True,
    ).start()
    applog.event(log, "justcall_webhook", accepted=True, justcall_id=cid)
    return {"ok": True, "queued": cid}


def _upload_error_status(msg: str) -> HTTPException:
    if "daily_cap_exceeded" in msg:
        return HTTPException(
            status_code=429,
            detail="Daily transcription cap reached (resets 00:00 UTC). "
                   "Try a fresh key or later.",
        )
    # Missing host key mentions transcribe:jobs in the copy — that is 502, not 403.
    if "PYAI_API_KEY not configured" in msg:
        return HTTPException(status_code=502, detail=msg)
    if "transcribe:jobs" in msg or "speaker-labelled" in msg:
        return HTTPException(status_code=403, detail=msg)
    return HTTPException(status_code=502, detail=f"Transcription failed: {msg}")


def _safe_zip_base_name(filename: str) -> str | None:
    raw = (filename or "").replace("\\", "/")
    if raw.startswith("/") or raw.startswith("..") or "/../" in f"/{raw}/":
        return None
    base = transcribe.sanitize_filename(os.path.basename(raw))
    ext = os.path.splitext(base)[1].lower()
    if ext == ".zip" or ext not in AUDIO_EXTS:
        return None
    return base


def _extract_batch_zip(zip_path: str, batch_dir: str) -> list[dict]:
    """Extract audio members into batch_dir. Raises HTTPException on bad zip."""
    extracted = []
    try:
        zf = zipfile.ZipFile(zip_path, "r")
    except zipfile.BadZipFile:
        raise HTTPException(status_code=400, detail="The upload was not a valid zip file.")

    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if not infos:
            raise HTTPException(status_code=400, detail="The zip did not contain any files.")
        if len(infos) > MAX_BULK_FILES:
            raise HTTPException(
                status_code=400,
                detail=f"Zip has too many files (max {MAX_BULK_FILES}).",
            )
        total_uncompressed = 0
        batch_abs = os.path.abspath(batch_dir) + os.sep
        for i, info in enumerate(infos):
            name = _safe_zip_base_name(info.filename)
            if not name:
                raise HTTPException(
                    status_code=400,
                    detail=f"Zip member is not an allowed audio file: {os.path.basename(info.filename)}",
                )
            if info.file_size > MAX_UPLOAD_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail=f"{name} is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
                )
            total_uncompressed += info.file_size
            if total_uncompressed > MAX_BATCH_ZIP_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail="Uncompressed zip contents exceed the batch size limit.",
                )
            display = name
            if len(name) > 3 and name[0:2].isdigit() and name[2] == "_":
                display = name[3:] or name
            dest = os.path.join(batch_dir, f"{i:02d}_{name}")
            dest_abs = os.path.abspath(dest)
            if not dest_abs.startswith(batch_abs):
                raise HTTPException(status_code=400, detail="Invalid zip member path.")
            copied = 0
            with zf.open(info, "r") as src, open(dest, "wb") as out:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"{name} exceeded the per-file size limit while extracting.",
                        )
                    out.write(chunk)
            extracted.append({"path": dest, "filename": display, "index": i})
    return extracted


@app.get("/api/calls/{call_id}/audio")
def get_audio(call_id: int, request: Request):
    """Return a time-limited signed URL. Membership is checked before signing."""
    org_id = _org(request)
    with _conn() as c:
        row = transcribe.get_call(c, call_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=404, detail="No audio for this call.")
    try:
        url, ttl = audio_store.signed_url(org_id, call_id)
    except audio_store.AudioNotFound:
        raise HTTPException(status_code=404, detail="No audio for this call.")
    except audio_store.AudioStoreError:
        raise HTTPException(status_code=503, detail="Audio storage is unavailable.")
    applog.event(log, "audio_signed", call_id=call_id, expires_in=ttl)
    return {"url": url, "expires_in": ttl}


@app.post("/api/calls/{call_id}/retranscribe")
def retranscribe_call(call_id: int, request: Request):
    """Re-run Hear on the stored recording (channel or diarize, never both)."""
    org_id = _org(request)
    if call_id < 1:
        raise HTTPException(status_code=400, detail="Invalid call id.")
    with _conn() as c:
        row = transcribe.get_call(c, call_id, org_id=org_id)
    if not row:
        raise HTTPException(status_code=404, detail="No stored audio for this call.")
    try:
        with audio_store.download_to_temp(org_id, call_id) as path:
            hear_tmp = f"{path}.{uuid.uuid4().hex}.hear.wav"
            size = os.path.getsize(path)
            try:
                pyai_id = transcribe.new_pyai_call_id()
                applog.event(
                    log, "retranscribe_started",
                    call_id=call_id, size_bytes=size,
                )
                job_id, result, mode = transcribe.transcribe_with_fallback(
                    path, hear_tmp, call_id=pyai_id,
                )
                with _db_lock:
                    with db.connection() as conn:
                        transcribe.replace_transcript(
                            conn, call_id, job_id, result,
                            pyai_call_id=pyai_id, org_id=org_id,
                        )
                audit, _rh = _load_or_compute_audit(call_id, org_id, refresh=True)
                applog.event(
                    log, "retranscribe_completed",
                    call_id=call_id, mode=mode, job_id=job_id,
                    segments=len(result.get("segments") or []),
                    score=audit.get("score") if isinstance(audit, dict) else None,
                )
                return {
                    "call_id": call_id,
                    "job_id": job_id,
                    "mode": mode,
                    "segments": len((audit or {}).get("segments") or []),
                    "score": (audit or {}).get("score"),
                }
            finally:
                if os.path.exists(hear_tmp):
                    os.remove(hear_tmp)
    except audio_store.AudioNotFound:
        raise HTTPException(
            status_code=404,
            detail="No stored audio for this call. Re-upload the file.",
        )
    except audio_store.AudioStoreError:
        raise HTTPException(status_code=503, detail="Audio storage is unavailable.")
    except HTTPException:
        raise
    except (Exception, SystemExit) as e:
        msg = str(e)
        log.error("retranscribe failed for call %s: %s", call_id, msg)
        raise HTTPException(status_code=502, detail="Retranscribe failed.") from e


@app.post("/api/upload")
def upload(request: Request, file: UploadFile = File(...)):
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    size = len(data)
    size_mb = size / (1024 * 1024)
    applog.event(
        log, "upload_received",
        filename=file.filename or "unknown",
        size_bytes=size,
        size_mb=round(size_mb, 3),
    )
    log.info("upload received: %s (%.2f MB)", file.filename, size_mb)
    if size > MAX_UPLOAD_BYTES:
        applog.event(
            log, "upload_rejected", level=logging.ERROR,
            filename=file.filename or "unknown",
            size_bytes=size,
            size_mb=round(size_mb, 3),
            error="file_too_large",
        )
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large for transcription ({size_mb:.1f} MB). "
                f"Maximum is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
            ),
        )

    tmp = _temp_audio_path()
    with open(tmp, "wb") as f:
        f.write(data)

    source_name = transcribe.sanitize_filename(file.filename)
    org_id = _org(request)

    try:
        call_id, _deduped = _ingest_audio_file(tmp, source_name, org_id=org_id)
        _store_playback(tmp, call_id, org_id)
    except HTTPException:
        raise
    except audio_store.AudioStoreError:
        raise HTTPException(status_code=503, detail="Audio storage is unavailable.")
    except (Exception, SystemExit) as e:
        msg = str(e)
        applog.event(
            log, "transcription_failure", level=logging.ERROR,
            filename=file.filename or "unknown",
            size_bytes=size,
            error=msg,
        )
        log.error("upload/transcription failed: %s", msg)
        sentry_report.capture_exception(e)
        raise _upload_error_status(msg)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return {"call_id": call_id, "filename": _call_filename(call_id, org_id)}


@app.post("/api/upload-batch")
def upload_batch(request: Request, file: UploadFile = File(...)):
    """
    One zip of up to MAX_BULK_FILES audio files. Extract to unique paths,
    transcribe all on PyAI in parallel, then run Claude QA in parallel.
    """
    data = file.file.read()
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded zip was empty.")
    if len(data) > MAX_BATCH_ZIP_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Zip is too large. Maximum is {MAX_BATCH_ZIP_BYTES // (1024 * 1024)} MB.",
        )

    batch_id = uuid.uuid4().hex
    batch_dir = tempfile.mkdtemp(prefix="callproof_batch_")
    zip_path = os.path.join(batch_dir, "batch.zip")
    with open(zip_path, "wb") as f:
        f.write(data)

    started = time.perf_counter()
    try:
        extracted = _extract_batch_zip(zip_path, batch_dir)
        applog.event(
            log, "batch_received",
            count=len(extracted),
            zip_bytes=len(data),
            batch_id=batch_id,
        )
        log.info("batch %s: %d file(s), parallel transcribe then parallel QA", batch_id, len(extracted))

        org_id = _org(request)
        ingest_rows = [None] * len(extracted)

        def ingest_one(item):
            try:
                with org_scope(org_id):
                    call_id, deduped = _ingest_audio_file(
                        item["path"], item["filename"], org_id=org_id,
                    )
                _store_playback(item["path"], call_id, org_id)
                return {
                    "index": item["index"],
                    "filename": item["filename"],
                    "call_id": call_id,
                    "deduped": deduped,
                    "error": None,
                }
            except (Exception, SystemExit) as e:  # noqa: BLE001
                msg = str(e)
                applog.event(
                    log, "transcription_failure", level=logging.ERROR,
                    filename=item["filename"],
                    error=msg,
                )
                sentry_report.capture_exception(e)
                return {
                    "index": item["index"],
                    "filename": item["filename"],
                    "call_id": None,
                    "deduped": False,
                    "error": msg,
                }

        workers = min(MAX_BULK_WORKERS, max(1, len(extracted)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(ingest_one, item) for item in extracted]
            for fut in as_completed(futs):
                row = fut.result()
                ingest_rows[row["index"]] = row

        to_audit = [r for r in ingest_rows if r and r.get("call_id") and not r.get("error")]

        def audit_one(row):
            try:
                with org_scope(org_id):
                    audit, _rh = _load_or_compute_audit(row["call_id"], org_id, refresh=False)
                return {
                    **row,
                    "status": "ok",
                    "score": audit.get("score"),
                    "grade": audit.get("grade"),
                    "flagged": bool(audit.get("flagged")),
                }
            except (Exception, SystemExit) as e:  # noqa: BLE001
                return {
                    **row,
                    "status": "error",
                    "error": f"Transcribed but audit failed: {e}",
                    "score": None,
                    "grade": None,
                    "flagged": False,
                }

        audited = {}
        if to_audit:
            with ThreadPoolExecutor(max_workers=min(MAX_BULK_WORKERS, len(to_audit))) as pool:
                futs = [pool.submit(audit_one, row) for row in to_audit]
                for fut in as_completed(futs):
                    row = fut.result()
                    audited[row["index"]] = row

        calls = []
        for row in ingest_rows:
            if not row:
                continue
            if row.get("error") and not row.get("call_id"):
                calls.append({
                    "filename": row["filename"],
                    "status": "error",
                    "error": row["error"],
                    "call_id": None,
                    "score": None,
                    "grade": None,
                    "flagged": False,
                    "deduped": False,
                })
            elif row["index"] in audited:
                out = audited[row["index"]]
                calls.append({
                    "filename": out["filename"],
                    "status": out.get("status") or "ok",
                    "error": out.get("error"),
                    "call_id": out.get("call_id"),
                    "score": out.get("score"),
                    "grade": out.get("grade"),
                    "flagged": bool(out.get("flagged")),
                    "deduped": bool(out.get("deduped")),
                })
            else:
                calls.append({
                    "filename": row["filename"],
                    "status": "ok",
                    "error": None,
                    "call_id": row.get("call_id"),
                    "score": None,
                    "grade": None,
                    "flagged": False,
                    "deduped": bool(row.get("deduped")),
                })

        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        applog.event(
            log, "batch_completed",
            batch_id=batch_id,
            count=len(calls),
            duration_ms=duration_ms,
        )
        log.info("batch %s done in %.0f ms (%d call(s))", batch_id, duration_ms, len(calls))
        return {"count": len(calls), "calls": calls}
    finally:
        shutil.rmtree(batch_dir, ignore_errors=True)


if not skip_startup():
    _start_justcall_poller()
