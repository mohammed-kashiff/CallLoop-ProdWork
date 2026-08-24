"""
CallProof - QA Engine (v3-capable, with logging).

Runs a config-driven rubric against a transcript and scores it deterministically.
All Claude calls use temperature=0 so a given transcript+rubric always produces
the same verdicts (and therefore the same score). Every failure is LOGGED, never
silently swallowed.

Criterion methods:
  deterministic | llm | deterministic_plus_llm | llm_plus_outcome_data
Verdicts: pass / partial / fail / unverified / not_applicable / error.
Scoring: pass=1.0, partial=0.5, fail=0.0 of weight. not_applicable / error / gates
(weight 0) are excluded from BOTH numerator and denominator (score renormalises).
"""

import os
import re
import sys
import json
import time
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import applog
from . import db
from . import pyai_usage
from . import qa_v8
from . import rules
from .config import load_env
from .paths import RUBRIC_PATH

load_env()
applog.setup_logging()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("callproof.qa")

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip() or None
MODEL = "claude-sonnet-5"


def set_api_key(api_key: str):
    """Inject / rotate the Anthropic key at runtime (used by the keys UI)."""
    global ANTHROPIC_API_KEY
    key = (api_key or "").strip()
    if not key:
        raise ValueError("ANTHROPIC_API_KEY cannot be empty")
    ANTHROPIC_API_KEY = key
    os.environ["ANTHROPIC_API_KEY"] = key
CLAUDE_EFFORT = "high"
# hybrid: channel/greeting roles; Claude for resolution + churn (parallel).
#         Tone LLM, ownership step-2, and first-load feedback stay skipped.
# full: same roles, plus tone LLM, ownership step-2 LLM, churn.
# Feedback is on-demand (Areas of Improvement), not part of the first-load wave.
_raw_mode = (os.getenv("AUDIT_MODE") or "hybrid").strip().lower()
AUDIT_MODE = _raw_mode if _raw_mode in ("hybrid", "full") else "hybrid"
if _raw_mode not in ("hybrid", "full"):
    log.warning("unknown AUDIT_MODE=%s; using hybrid", _raw_mode)
MAX_HTTP_RETRIES = 4       # attempts per Claude call (with backoff)
MAX_PARSE_RETRIES = 2      # re-asks if the reply isn't valid JSON
MAX_TOKENS = 2000


# ---------- Load transcript ----------
def load_call(call_id=None):
    with db.connection() as conn:
        if call_id is None:
            row = conn.execute(
                "SELECT id FROM calls WHERE status='completed' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                sys.exit("No completed calls in the database. Run transcribe.py first.")
            call_id = row["id"]
        meta = conn.execute(
            """
            SELECT id, full_text, speakers, audio_seconds, pyai_call_id
            FROM calls WHERE id = %s
            """,
            (call_id,),
        ).fetchone()
        if not meta:
            sys.exit(f"No call with id {call_id} in the database.")
        segs = conn.execute(
            """
            SELECT seq, speaker, channel, "start", "end", text
            FROM segments WHERE call_id = %s ORDER BY seq
            """,
            (call_id,),
        ).fetchall()
    return call_id, dict(meta), [dict(s) for s in segs]


def identify_agent(segments):
    return segments[0]["speaker"] if segments else None


def audit_mode() -> str:
    return AUDIT_MODE


def is_hybrid_audit() -> bool:
    return AUDIT_MODE == "hybrid"


# Agent vs customer cues for the first window of the call (no LLM).
_AGENT_CUES = (
    "thank you for calling",
    "thanks for calling",
    "thank you for contacting",
    "how can i help",
    "how may i help",
    "how can i assist",
    "how may i assist",
    "my name is",
    "calling from",
    "speaking with",
    "welcome to",
    "on behalf of",
    "let me pull up",
    "i've pulled up",
    "for your security",
    "verify your",
    "i can look into",
    "i can help you with",
)
_CUSTOMER_CUES = (
    "i'm calling",
    "i am calling",
    "i was calling",
    "calling about",
    "my account",
    "my order",
    "my bill",
    "can you help me",
    "i need help",
    "i've been trying",
    "this is the third",
    "i want to cancel",
    "i need to speak",
    "you guys",
    "your company",
)


def _cue_score(text: str) -> int:
    t = (text or "").lower()
    score = 0
    for cue in _AGENT_CUES:
        if cue in t:
            score += 2
    for cue in _CUSTOMER_CUES:
        if cue in t:
            score -= 2
    return score


def _channel_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def classify_roles(segments):
    """Identify the AGENT without Claude.

    Prefer greeting/company cues in the opening turns. If those tie, use Hear
    channel (lower channel index = agent, typical left/agent dual-channel).
    Last resort: first speaker.
    """
    if not segments:
        return None
    speakers = []
    for s in segments:
        sp = s.get("speaker")
        if sp and sp not in speakers:
            speakers.append(sp)
    if len(speakers) < 2:
        agent = speakers[0] if speakers else identify_agent(segments)
        applog.event(log, "role_classified", method="single_speaker", agent=agent)
        log.info("role classification: agent = %s (single speaker)", agent)
        return agent

    window = [s for s in segments[:15] if s.get("speaker")]
    scores = {sp: 0 for sp in speakers}
    channel_of = {sp: [] for sp in speakers}
    for s in window:
        sp = s["speaker"]
        scores[sp] = scores.get(sp, 0) + _cue_score(s.get("text") or "")
        ch = _channel_int(s.get("channel"))
        if ch is not None:
            channel_of.setdefault(sp, []).append(ch)

    best = max(speakers, key=lambda sp: scores.get(sp, 0))
    worst = min(speakers, key=lambda sp: scores.get(sp, 0))
    if scores[best] > scores[worst]:
        applog.event(
            log, "role_classified",
            method="greeting", agent=best, score=scores[best],
        )
        log.info("role classification: agent = %s (greeting cues)", best)
        return best

    # Distinct channels: speaker whose median/mode channel is the lower index.
    mode_ch = {}
    for sp, chans in channel_of.items():
        if not chans:
            continue
        mode_ch[sp] = max(set(chans), key=chans.count)
    if len(mode_ch) >= 2 and len(set(mode_ch.values())) >= 2:
        agent = min(mode_ch, key=lambda sp: mode_ch[sp])
        applog.event(
            log, "role_classified",
            method="channel", agent=agent, channel=mode_ch[agent],
        )
        log.info(
            "role classification: agent = %s (channel %s)",
            agent, mode_ch[agent],
        )
        return agent

    agent = identify_agent(segments)
    applog.event(log, "role_classified", method="first_speaker", agent=agent)
    log.info("role classification: agent = %s (first speaker fallback)", agent)
    return agent


def format_transcript(segments, agent_speaker):
    lines = []
    for s in segments:
        who = "AGENT" if s["speaker"] == agent_speaker else "CUSTOMER"
        start = s["start"] if s["start"] is not None else 0.0
        lines.append(f'[seq {s["seq"]}] ({who}, {start:.1f}s) {s["text"]}')
    return "\n".join(lines)


# ---------- Evidence-validation gate ----------
def _norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def validate_evidence(quote, segments):
    q = _norm(quote)
    if not q:
        return False, None
    for s in segments:
        if q in _norm(s["text"]):
            return True, s["seq"]
    return False, None


# ---------- JSON parsing (robust) ----------
def _iter_json_objects(text):
    t = text or ""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield t[start:i + 1]
                    start = None


def parse_json(text):
    for obj in _iter_json_objects((text or "").strip()):
        try:
            return json.loads(obj)
        except Exception:  # noqa: BLE001
            continue
    raise ValueError("no parseable JSON object found")


# ---------- LLM plumbing ----------
SYSTEM_INSTRUCTIONS = (
    "You are a strict call-quality auditor. Evaluate ONLY the AGENT on the one "
    "criterion given, using only the transcript provided. When your verdict is "
    "pass/partial/fail you MUST cite a real, exact quote copied verbatim from a "
    "transcript line. Never invent or paraphrase a quote. Respond with JSON only."
)


def _criterion_question(cr):
    if cr.get("question"):
        return cr["question"]
    steps = [s for s in (cr.get("question_step_1"), cr.get("question_step_2")) if s]
    if steps:
        return "\n".join(f"Step {i}: {s}" for i, s in enumerate(steps, 1))
    if cr.get("llm_question"):
        return cr["llm_question"]
    return None


def build_prompt(question, transcript_text, allowed_verdicts, strict=False):
    verdicts = " | ".join(f'"{v}"' for v in allowed_verdicts)
    na_note = ""
    if "not_applicable" in allowed_verdicts:
        na_note = ('\nIf this criterion does not apply to this call, return '
                   '"not_applicable" with a brief reason and an empty evidence_quote.')
    base = (
        f"{SYSTEM_INSTRUCTIONS}\n\nCRITERION:\n{question}\n\n"
        f"TRANSCRIPT (one turn per line):\n{transcript_text}\n\n"
        f"Your verdict MUST be one of: {verdicts}.{na_note}\n"
        "Return ONLY this JSON object:\n{\n"
        f'  "verdict": one of {verdicts},\n'
        '  "reasoning": "one or two sentences",\n'
        '  "evidence_quote": "a SHORT exact span, 5-15 words, copied verbatim from one transcript line",\n'
        '  "evidence_seq": <the seq number of the line you quoted>\n}'
    )
    if strict:
        base += "\n\nYour previous reply could not be parsed. Output ONLY raw JSON, no markdown, no commentary."
    return base


def _claude_json_body(prompt: str, model=None, effort=None, max_tokens=None) -> dict:
    """Sonnet 5+ accepts output_config.effort. Haiku 4.5 rejects it."""
    model = model or MODEL
    body = {
        "model": model,
        "max_tokens": max_tokens or MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    if "haiku" not in (model or "").lower():
        body["thinking"] = {"type": "disabled"}
        body["output_config"] = {"effort": effort or CLAUDE_EFFORT}
    return body


def call_claude(prompt, model=None, effort=None, max_tokens=None, timeout=60):
    """POST to Claude with temperature=0. Retries with backoff on 429/5xx.
    Logs every failed attempt. Raises RuntimeError only if all attempts fail."""
    if not ANTHROPIC_API_KEY:
        applog.event(
            log, "claude_failure", level=logging.ERROR,
            error="ANTHROPIC_API_KEY is not set",
        )
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    last_err = None
    started = time.perf_counter()
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            resp = pyai_usage.post(
                "https://api.anthropic.com/v1/messages",
                provider="anthropic",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=_claude_json_body(
                    prompt, model=model, effort=effort, max_tokens=max_tokens,
                ),
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                applog.event(
                    log, "claude_success",
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    chars=len(text),
                )
                return text
            last_err = f"{resp.status_code}: {resp.text[:300]}"
            log.warning("claude attempt %d/%d -> %s", attempt, MAX_HTTP_RETRIES, last_err)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 * attempt)          # 2s, 4s, 6s ... backoff
                continue
            break                                # 400/401/403: retrying won't help
        except Exception as e:  # noqa: BLE001
            last_err = applog.safe_exception_text(e)
            log.warning("claude attempt %d/%d exception: %s", attempt, MAX_HTTP_RETRIES, last_err)
            time.sleep(1)
    applog.event(
        log, "claude_failure", level=logging.ERROR,
        attempts=MAX_HTTP_RETRIES,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        error=last_err,
    )
    log.error("claude call failed after %d attempts: %s", MAX_HTTP_RETRIES, last_err)
    raise RuntimeError(f"Claude call failed: {last_err}")


def run_llm_criterion(criterion, transcript_text, segments):
    name = criterion.get("name", criterion.get("id", "?"))
    question = _criterion_question(criterion)
    if not question:
        log.error("criterion '%s' has no LLM question defined", name)
        return {"verdict": "error", "reasoning": "No LLM question defined for this criterion.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": False}
    allowed = criterion.get("verdict_space", ["pass", "partial", "fail"])

    parsed = None
    for attempt in range(MAX_PARSE_RETRIES):
        try:
            raw = call_claude(build_prompt(question, transcript_text, allowed, strict=(attempt > 0)))
        except Exception as e:  # noqa: BLE001
            err = applog.safe_exception_text(e)
            log.error("criterion '%s' LLM call failed: %s", name, err)
            return {"verdict": "error", "reasoning": f"LLM call failed: {err}",
                    "evidence_text": None, "evidence_seq": None, "evidence_verified": False}
        try:
            parsed = parse_json(raw)
            break
        except Exception:  # noqa: BLE001
            log.warning("criterion '%s' returned unparseable JSON (attempt %d)", name, attempt + 1)
            parsed = None
    if parsed is None:
        log.error("criterion '%s' -> error (no valid JSON after retries)", name)
        return {"verdict": "error", "reasoning": "Model output was not valid JSON after a retry.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": False}

    verdict = parsed.get("verdict", "error")
    if verdict == "not_applicable":
        log.info("criterion '%s' -> not_applicable", name)
        return {"verdict": "not_applicable", "reasoning": parsed.get("reasoning", ""),
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}

    quote = parsed.get("evidence_quote", "")
    verified, seq = validate_evidence(quote, segments)
    if criterion.get("evidence_required", True) and not verified:
        log.info("criterion '%s' -> UNVERIFIED (quote not found in transcript)", name)
        return {"verdict": "unverified", "reasoning": parsed.get("reasoning", ""),
                "evidence_text": quote, "evidence_seq": parsed.get("evidence_seq"),
                "evidence_verified": False, "original_verdict": verdict}
    log.info("criterion '%s' -> %s (evidence verified)", name, verdict)
    return {"verdict": verdict, "reasoning": parsed.get("reasoning", ""),
            "evidence_text": quote, "evidence_seq": seq, "evidence_verified": verified}


def run_deterministic_criterion(criterion, segments, agent_speaker):
    name = criterion.get("name", criterion.get("id", "?"))
    fn = rules.REGISTRY.get(criterion["check"])
    if not fn:
        log.error("criterion '%s' references unknown rule '%s'", name, criterion["check"])
        return {"verdict": "error", "reasoning": f"Unknown rule '{criterion['check']}'.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}
    try:
        r = fn(segments, agent_speaker)
    except Exception as e:  # noqa: BLE001
        log.error("rule '%s' raised: %s", criterion["check"], applog.safe_exception_text(e))
        return {"verdict": "error", "reasoning": "Rule crashed.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}
    log.info("criterion '%s' -> %s (rule)", name, r["verdict"])
    return {"verdict": r["verdict"], "reasoning": r["reasoning"],
            "evidence_text": r["evidence_text"], "evidence_seq": r["evidence_seq"],
            "evidence_verified": r["evidence_text"] is not None}


def run_combined_criterion(criterion, segments, agent_speaker, transcript_text):
    det = run_deterministic_criterion(criterion, segments, agent_speaker)
    llm_q = criterion.get("llm_question")
    if not llm_q:
        return det
    llm_cr = dict(criterion)
    llm_cr["question"] = llm_q
    llm_cr["verdict_space"] = ["pass", "fail"]
    llm = run_llm_criterion(llm_cr, transcript_text, segments)
    if det["verdict"] == "fail":
        return det
    if llm["verdict"] == "fail":
        return llm
    return det


def evaluate_criterion(criterion, segments, agent_speaker, transcript_text):
    method = criterion.get("method")
    if method == "deterministic":
        return run_deterministic_criterion(criterion, segments, agent_speaker)
    if method == "deterministic_plus_llm":
        return run_combined_criterion(criterion, segments, agent_speaker, transcript_text)
    return run_llm_criterion(criterion, transcript_text, segments)


def evaluate_all_criteria(criteria, segments, agent_speaker, transcript_text, max_workers=None):
    """Evaluate every rubric criterion concurrently (one Claude call per LLM criterion)."""
    n = len(criteria)
    if n == 0:
        return []
    workers = max_workers or min(16, n)
    ordered = [None] * n
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                evaluate_criterion, cr, segments, agent_speaker, transcript_text
            ): (i, cr)
            for i, cr in enumerate(criteria)
        }
        for fut in as_completed(futs):
            i, cr = futs[fut]
            ordered[i] = (cr, fut.result())
    log.info(
        "evaluated %d criteria in parallel in %.1fs (workers=%d)",
        n, time.perf_counter() - t0, workers,
    )
    return ordered


def draft_retention_email(transcript_text, segments):
    """
    Draft a stakeholder retention email from the transcript (one Claude call).
    On-demand only — not part of the first-load audit wave.
    """
    prompt = (
        "You are a customer-retention specialist writing an INTERNAL email a manager can send "
        "to a stakeholder about retaining this customer after the call below.\n\n"
        "Analyze the transcript for churn risk, unmet needs, frustration, competitors, "
        "pricing/product issues, and what concrete retention steps would help.\n"
        "Use ONLY facts supported by the transcript. Do not invent discounts, credits, or "
        "commitments that were not discussed. Keep the tone professional and actionable.\n\n"
        f"TRANSCRIPT (one turn per line):\n{transcript_text}\n\n"
        "Return ONLY this JSON:\n"
        "{"
        '"subject": "short email subject", '
        '"body": "plain-text email body with short paragraphs and a clear ask/next steps", '
        '"summary": "1-2 sentence situation summary", '
        '"suggested_actions": ["specific next step 1", "specific next step 2"]'
        "}"
    )
    try:
        parsed = parse_json(call_claude(prompt))
    except Exception as e:  # noqa: BLE001
        err = applog.safe_exception_text(e)
        log.error("retention email draft failed: %s", err)
        return {
            "status": "error",
            "error": err,
            "subject": "",
            "body": "",
            "summary": "",
            "suggested_actions": [],
        }

    subject = (parsed.get("subject") or "").strip()
    body = (parsed.get("body") or "").strip()
    summary = (parsed.get("summary") or "").strip()
    actions = parsed.get("suggested_actions") or []
    if not isinstance(actions, list):
        actions = []
    actions = [str(a).strip() for a in actions if str(a).strip()]

    # Light grounding: if body empty, treat as failure for compose fallback.
    if not body:
        log.warning("retention email draft returned empty body")
        return {
            "status": "error",
            "error": "Model returned an empty retention email body.",
            "subject": subject,
            "body": "",
            "summary": summary,
            "suggested_actions": actions,
        }

    log.info(
        "retention email drafted (%d chars, %d actions)",
        len(body), len(actions),
    )
    return {
        "status": "ok",
        "subject": subject,
        "body": body,
        "summary": summary,
        "suggested_actions": actions,
    }


def run_parallel_claude_wave(criteria, segments, agent_speaker, transcript_text, max_workers=None, rubric=None):
    """
    Fire independent Claude work in one parallel wave (dimensions/criteria +
    churn). Retention email and areas of improvement are on-demand.
    Hybrid mode: Claude for resolution + churn (parallel); skip tone/ownership step-2.
    """
    mode = audit_mode()
    if rubric is not None and qa_v8.is_v8_rubric(rubric):
        ordered, churn, feedback, retention_email, score, tally, grade, hostile, manager_review = (
            qa_v8.run_v8_wave(
                rubric, segments, agent_speaker, transcript_text,
                call_claude, parse_json, build_prompt, validate_evidence,
                assess_churn, extract_feedback,
                max_workers=max_workers,
                audit_mode=mode,
            )
        )
        return {
            "mode": "v8",
            "audit_mode": mode,
            "results": ordered,
            "churn": churn,
            "feedback": feedback,
            "retention_email": retention_email,  # None — drafted on Email stakeholder
            "score": score,
            "tally": tally,
            "grade": grade,
            "hostile": hostile,
            "manager_review": manager_review,
            "findings": qa_v8.findings_from_v8(ordered),
        }

    n = len(criteria)
    workers = max_workers or min(32, max(4, n + 1))
    ordered = [None] * n
    churn = None
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for i, cr in enumerate(criteria):
            futs[pool.submit(
                evaluate_criterion, cr, segments, agent_speaker, transcript_text
            )] = ("crit", i, cr)
        futs[pool.submit(assess_churn, transcript_text, segments)] = ("churn", None, None)
        for fut in as_completed(futs):
            kind, i, cr = futs[fut]
            if kind == "crit":
                ordered[i] = (cr, fut.result())
            else:
                churn = fut.result()
    log.info(
        "parallel Claude wave done in %.1fs "
        "(%d criteria + churn, workers=%d)",
        time.perf_counter() - t0, n, workers,
    )
    return {
        "mode": "v3",
        "results": ordered,
        "churn": churn,
        "feedback": {
            "status": "skipped",
            "reason": "on_demand",
            "agent": [],
            "product": [],
        },
        "retention_email": None,
    }


# ---------- Scoring ----------
FRACTION = {"pass": 1.0, "partial": 0.5, "fail": 0.0, "unverified": 0.0}
SCORE_EXCLUDED = {"not_applicable", "error"}


def performance_band(score, rubric=None):
    """Keep numeric score primary; band is a display label.
    Prefers v8 rubric bands when available; falls back to legacy labels."""
    if rubric is not None and qa_v8.is_v8_rubric(rubric):
        return qa_v8.performance_band(score, rubric)
    # Legacy labels (still available for old cached audits / v3 rubrics)
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 60:
        return "Needs improvement"
    return "Poor"


def awarded_points(criterion, verdict):
    if criterion.get("is_gate") or criterion.get("weight", 0) == 0:
        return None
    if verdict in SCORE_EXCLUDED:
        return None
    return round(criterion["weight"] * FRACTION.get(verdict, 0.0), 1)


def score_results(results):
    rows, earned, possible, tally, gate_fails = [], 0.0, 0.0, {}, []
    for cr, res in results:
        v = res["verdict"]
        tally[v] = tally.get(v, 0) + 1
        if cr.get("is_gate") and v == "fail":
            gate_fails.append(cr["name"])
        pts = awarded_points(cr, v)
        rows.append((cr, res, pts))
        if pts is not None:
            earned += pts
            possible += cr["weight"]
    score = round(earned / possible * 100, 1) if possible else 0.0
    if "error" in tally:
        log.warning("%d criteria errored and were EXCLUDED from the score", tally["error"])
    log.info("score: %s/%s weighted -> %s/100 (%s); tally=%s; gates_failed=%s",
             earned, possible, score, performance_band(score), tally, gate_fails or "none")
    return rows, score, round(earned, 1), round(possible, 1), tally, gate_fails


def _feedback_item(summary, sentiment, quote, seq, verified):
    return {
        "summary": summary or "",
        "sentiment": sentiment if sentiment in ("positive", "negative", "neutral") else "neutral",
        "quote": quote or None,
        "seq": seq if verified else None,
        "verified": verified if quote else None,
    }


def _parse_feedback_bucket(raw_items, segments):
    items = []
    for it in raw_items or []:
        if not isinstance(it, dict):
            continue
        quote = (it.get("quote") or "")
        verified, seq = validate_evidence(quote, segments) if quote else (False, None)
        summary = (it.get("summary") or "").strip()
        if not summary and not quote:
            continue
        items.append(_feedback_item(
            summary, it.get("sentiment", "neutral"), quote, seq, verified,
        ))
    return items


def _agent_insights_from_findings(findings, segments):
    """Guarantee the agent box is never empty: use scored gaps, else a pass note."""
    items = []
    for f in findings or []:
        if not isinstance(f, dict):
            continue
        if (f.get("verdict") or "") not in ("partial", "fail"):
            continue
        quote = f.get("evidence_text") or ""
        seq = f.get("evidence_seq")
        verified = False
        if quote:
            verified, found = validate_evidence(quote, segments)
            if verified:
                seq = found
        why = (f.get("why") or f.get("reasoning") or "").strip()
        name = f.get("name") or f.get("id") or "this dimension"
        summary = why or f"Improve {name}: the agent did not fully meet this dimension."
        items.append(_feedback_item(
            summary,
            "negative" if f.get("verdict") == "fail" else "neutral",
            quote, seq, verified,
        ))
        if len(items) >= 3:
            break
    if items:
        return items
    return [_feedback_item(
        "No major agent gaps stood out on this call. Keep the listening, ownership, "
        "and tone behaviors that scored a pass.",
        "positive", None, None, None,
    )]


def extract_feedback(transcript_text, segments, findings=None):
    """Areas of improvement: agent insights (always) + product comments (if any).

    Uses Claude Sonnet at effort=high. Agent bucket is required even when the
    customer never commented on the agent — infer from the agent's turns.
    """
    prompt = (
        "You are a call-quality coach writing AREAS OF IMPROVEMENT for this call.\n\n"
        "Fill two buckets:\n"
        "- 'agent' (REQUIRED, 1 to 3 items): concrete insights about how the AGENT handled "
        "the call — listening, ownership, tone, resolution, next steps. Do NOT leave this "
        "empty. Prefer what the customer reacted to. If the customer never commented on the "
        "agent, infer from the agent's own turns. Each summary is 1-2 sentences of coaching "
        "insight, not a restatement of the quote.\n"
        "- 'product' (0 or more): only what the CUSTOMER said about the product, pricing, "
        "features, reliability, or bugs. Empty list is OK.\n\n"
        "Quotes must be copied verbatim from one transcript line. For an inferred agent "
        "insight, quote the AGENT line the insight refers to. Never invent a quote.\n\n"
        f"TRANSCRIPT (one turn per line):\n{transcript_text}\n\n"
        "Return ONLY this JSON:\n"
        '{"agent": [{"summary":"1-2 sentence insight","sentiment":"positive|negative|neutral",'
        '"quote":"exact transcript span","seq":<seq or null>}], "product":[ ...same shape... ]}'
    )
    model = "claude-sonnet-5"
    effort = "high"
    try:
        parsed = parse_json(call_claude(
            prompt,
            model=model,
            effort=effort,
            max_tokens=3000,
            timeout=90,
        ))
    except Exception as e:  # noqa: BLE001
        log.error("feedback extraction failed: %s", applog.safe_exception_text(e))
        agent = _agent_insights_from_findings(findings, segments)
        return {
            "status": "ok",
            "agent": agent,
            "product": [],
            "source": "findings_fallback",
        }
    out = {
        "agent": _parse_feedback_bucket(parsed.get("agent"), segments),
        "product": _parse_feedback_bucket(parsed.get("product"), segments),
    }
    if not out["agent"]:
        out["agent"] = _agent_insights_from_findings(findings, segments)
    applog.event(
        log, "feedback_model",
        model=model, effort=effort,
        agent_items=len(out["agent"]),
        product_items=len(out["product"]),
    )
    log.info(
        "areas of improvement via %s effort=%s: %d agent, %d product",
        model, effort, len(out["agent"]), len(out["product"]),
    )
    out["status"] = "ok"
    return out


CHURN_LEVELS = ["none", "low", "medium", "high"]


def assess_churn(transcript_text, segments):
    """One LLM pass: rate churn risk and cite the customer's own words. Evidence-validated."""
    prompt = (
        "Analyze this customer service call for CHURN RISK - signs the customer may stop doing "
        "business: cancelling, downgrading, switching to a competitor, strong dissatisfaction, "
        "repeated unresolved issues, or threats to leave. Rate the risk and cite the customer's "
        "exact words.\n\n"
        f"TRANSCRIPT (one turn per line):\n{transcript_text}\n\n"
        'Return ONLY this JSON:\n'
        '{"risk": "none|low|medium|high", "reasoning": "one or two sentences", '
        '"evidence_quote": "exact customer line showing risk, or empty if none", '
        '"evidence_seq": <seq number or null>}'
    )
    try:
        parsed = parse_json(call_claude(prompt))
    except Exception as e:  # noqa: BLE001
        log.error("churn assessment failed: %s", applog.safe_exception_text(e))
        return {"risk": "unknown", "reasoning": "Could not assess churn.",
                "evidence_text": None, "evidence_seq": None, "evidence_verified": None}
    risk = parsed.get("risk", "unknown")
    if risk not in CHURN_LEVELS:
        risk = "unknown"
    quote = parsed.get("evidence_quote", "") or ""
    verified, seq = validate_evidence(quote, segments) if quote else (False, None)
    log.info("churn risk assessed: %s", risk)
    return {"risk": risk, "reasoning": parsed.get("reasoning", ""),
            "evidence_text": quote or None,
            "evidence_seq": seq if verified else None,
            "evidence_verified": verified if quote else None}


LABEL = {"pass": "PASS", "partial": "PARTIAL", "fail": "FAIL",
         "unverified": "UNVERIFIED", "not_applicable": "N/A", "error": "ERROR"}


def main():
    if not ANTHROPIC_API_KEY:
        sys.exit("ERROR: ANTHROPIC_API_KEY not found in .env")
    arg_id = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else None
    agent_override = sys.argv[2] if len(sys.argv) > 2 else None

    call_id, meta, segments = load_call(arg_id)
    if not segments:
        sys.exit(f"Call {call_id} has no segments to analyze.")
    agent = agent_override or classify_roles(segments)
    transcript_text = format_transcript(segments, agent)
    with open(RUBRIC_PATH) as f:
        rubric = json.load(f)

    log.info("auditing call %d (%ss, %d turns) against '%s'",
             call_id, meta.get("audio_seconds"), len(segments), rubric["name"])

    if qa_v8.is_v8_rubric(rubric):
        wave = run_parallel_claude_wave(
            [], segments, agent, transcript_text, rubric=rubric,
        )
        results = wave["results"]
        score = wave["score"]
        grade = wave["grade"]
        tally = wave["tally"]
        manager_review = wave.get("manager_review") or []
        print("=" * 72)
        for dim, res in results:
            weight = dim.get("weight") or 0
            v = res.get("verdict")
            frac = FRACTION.get(v)
            pt = "  -  " if frac is None else f"{round(weight * frac, 1):>5}/{weight}"
            print(
                f"[{dim.get('method')}] {dim['name']} -> "
                f"{LABEL.get(v, v)}  {pt}"
            )
        if manager_review:
            reasons = ", ".join(t.get("reason", "?") for t in manager_review)
            print(f"\n!! MANAGER REVIEW: {reasons}")
        print(f"TOTAL: {score}/100 -> {grade.upper()}  tally={tally}")
        print("=" * 72)
    else:
        results = evaluate_all_criteria(
            rubric["criteria"], segments, agent, transcript_text
        )
        rows, score, earned, possible, tally, gate_fails = score_results(results)
        grade = performance_band(score, rubric)

        print("=" * 72)
        for c, res, pts in rows:
            gate = " [GATE]" if c.get("is_gate") else ""
            pt = "  -  " if pts is None else f"{pts:>5}/{c['weight']}"
            print(f"[{c['method']}]{gate} {c['name']} -> {LABEL.get(res['verdict'], res['verdict'])}  {pt}")
        if gate_fails:
            print(f"\n!! GATE FAILURE - flag for manager review: {', '.join(gate_fails)}")
        print(f"TOTAL: {score}/100 ({earned} of {possible} weighted points) -> {grade.upper()}")
        print("=" * 72)


if __name__ == "__main__":
    main()