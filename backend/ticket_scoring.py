"""Ticket Audit Engine (TA-6): content-blind scoring of a sequenced ticket.

The call-scoring engine's mechanism — judge a criterion against speaker-labeled
turns, cite a quote, verify it — is channel-agnostic. That does not mean the
same files should run both. This module is the ticket engine's own evaluation
loop. It imports exactly three symbols from qa_engine.py:

    build_prompt, call_claude, validate_evidence

and nothing else from qa_engine.py, qa_v8.py, or rules_v8.py. Those three are
the shared primitives named in the Ticket Audit Engine PRD §3 / §9: a question
plus sequenced text in, a verdict plus a checkable quote out. No call-specific
logic lives in this file (no hostile-language gate, no rules_v8 dispatch, no
run_v8_wave).

v1 scores the whole thread once. Multi-agent span logic (TA-8) still runs on
every score: every turn already carries agent_user_id (nullable), every finding
is attributed via evidence_seq to the span that turn falls in, and the audit
as a whole is assigned a single primary owner. Per-span re-scoring is v2.

Image-derived turns (TA-5) arrive as ordinary sequenced text — a vision
description injected at the right seq — so this module has no reason to know
a given turn originated from a picture. A finding that cites that seq is what
lets a reviewer open the stored screenshot next to the verdict.
"""

from __future__ import annotations

import json
import logging
from collections import Counter

from . import applog
from .qa_engine import build_prompt, call_claude, validate_evidence

log = logging.getLogger("callproof.ticket_scoring")

ALLOWED_VERDICTS = ("pass", "partial", "fail")
POINTS = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
_SKIP_SCORE = frozenset({"not_applicable", "error", "unverified"})


def format_turns(turns: list[dict]) -> str:
    """Sequenced text the shared primitives already know how to judge.

    One line per turn, speaker-labeled, no timestamps — tickets are async
    and have no call clock. Image-description turns look like every other
    line because they already are text by the time they get here.
    """
    lines = []
    for t in sorted(turns, key=lambda row: row["seq"]):
        text = (t.get("text") or "").replace("\r\n", "\n").replace("\n", " ").strip()
        speaker = t.get("speaker") or "unknown"
        lines.append(f'[seq {t["seq"]}] ({speaker}) {text}')
    return "\n".join(lines)


def agent_spans(turns: list[dict]) -> list[dict]:
    """Split a thread into agent-owned spans.

    A span starts when an agent speaks and runs until a *different* agent
    speaks. Customer and bot turns in between stay inside the current span
    — that's the stretch of thread that agent was responsible for. Two
    agent turns with the same agent_user_id (including both NULL) merge;
    a change of agent_user_id opens a new span.

    v1 does not re-score per span. The spans are captured so a finding's
    evidence_seq can be attributed, and so v2 can score them independently
    without a schema rebuild.
    """
    spans: list[dict] = []
    current: dict | None = None
    for t in sorted(turns, key=lambda row: row["seq"]):
        seq = t["seq"]
        if t.get("speaker") == "agent":
            uid = t.get("agent_user_id")
            if current is None or current["agent_user_id"] != uid:
                if current is not None:
                    spans.append(current)
                current = {
                    "agent_user_id": uid,
                    "start_seq": seq,
                    "end_seq": seq,
                    "turn_count": 1,
                }
            else:
                current["end_seq"] = seq
                current["turn_count"] += 1
        elif current is not None:
            current["end_seq"] = seq
    if current is not None:
        spans.append(current)
    return spans


def primary_owner(turns: list[dict]) -> str | None:
    """v1 single-owner attribution for the whole-thread audit.

    Prefer whoever sent the last agent message (the resolving turn). If
    that turn has no agent_user_id — the PDF parser's current state — fall
    back to whoever has the most agent turns among those that do have an
    id. None when no agent turn is identifiable.
    """
    agents = [t for t in sorted(turns, key=lambda row: row["seq"])
              if t.get("speaker") == "agent"]
    if not agents:
        return None
    last_id = agents[-1].get("agent_user_id")
    if last_id:
        return last_id
    counts = Counter(t.get("agent_user_id") for t in agents if t.get("agent_user_id"))
    if not counts:
        return None
    best = max(counts.values())
    for t in agents:
        uid = t.get("agent_user_id")
        if uid and counts[uid] == best:
            return uid
    return None


def attributed_agent(turns: list[dict], evidence_seq, spans: list[dict] | None = None):
    """The agent who owns the span containing evidence_seq.

    Falls out of evidence verification rather than a bespoke mechanism:
    a verified quote points at a seq, that seq sits in exactly one span.
    Customer/bot evidence still attributes to the agent who owned that
    stretch of the thread. Pre-first-agent seqs are unattributed.
    """
    if evidence_seq is None:
        return None
    try:
        seq = int(evidence_seq)
    except (TypeError, ValueError):
        return None
    for span in (spans if spans is not None else agent_spans(turns)):
        if span["start_seq"] <= seq <= span["end_seq"]:
            return span["agent_user_id"]
    return None


def _parse_json(text: str) -> dict:
    """Local JSON extractor — not qa_engine.parse_json, which this module
    is not allowed to import."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        obj = json.loads(raw[start:end + 1])
        if isinstance(obj, dict):
            return obj
    raise ValueError("no parseable JSON object found")


def evaluate_criterion(
    question: str,
    turns: list[dict],
    *,
    allowed_verdicts: tuple[str, ...] = ALLOWED_VERDICTS,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> dict:
    """One question against sequenced text. Verdict + checkable quote.

    Content-blind: the same function scores a typed reply and an
    image-description turn, because both are just lines in `turns`.
    """
    question = (question or "").strip()
    if not question:
        return {
            "verdict": "error",
            "reasoning": "This criterion has no question text.",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
        }
    if not turns:
        return {
            "verdict": "error",
            "reasoning": "No turns to score.",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
        }

    transcript_text = format_turns(turns)
    prompt = build_prompt_fn(question, transcript_text, list(allowed_verdicts))
    try:
        raw = call_claude_fn(prompt)
        try:
            parsed = _parse_json(raw)
        except ValueError:
            raw = call_claude_fn(
                build_prompt_fn(question, transcript_text, list(allowed_verdicts), strict=True),
            )
            parsed = _parse_json(raw)
    except Exception as exc:  # noqa: BLE001
        err = applog.safe_exception_text(exc)
        applog.event(log, "ticket_criterion_failed", level=logging.ERROR, error=err)
        return {
            "verdict": "error",
            "reasoning": f"LLM step failed: {err}",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
        }

    quote = parsed.get("evidence_quote") or parsed.get("evidence_text") or ""
    verified, seq = validate_evidence_fn(quote, turns)
    verdict = parsed.get("verdict", "error")
    if verdict not in allowed_verdicts and verdict not in ("not_applicable", "error"):
        verdict = "error"
    claimed = parsed.get("evidence_seq")
    try:
        claimed_seq = int(claimed) if claimed is not None else None
    except (TypeError, ValueError):
        claimed_seq = None
    return {
        "verdict": verdict,
        "reasoning": parsed.get("reasoning") or "",
        "evidence_text": quote or None,
        "evidence_seq": seq if verified else claimed_seq,
        "evidence_verified": bool(verified),
    }


def _dimension_question(dim: dict) -> str:
    return (dim.get("question") or dim.get("llm_question") or "").strip()


def run_ticket_wave(
    turns: list[dict],
    dimensions: list[dict],
    *,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> list[dict]:
    """Ticket engine's own evaluation loop. Not run_v8_wave.

    Scores the whole thread once per dimension (v1). Span split and
    primary-owner assignment happen after, in score_ticket — they do not
    change how many Claude calls fire.
    """
    findings = []
    for dim in dimensions:
        result = evaluate_criterion(
            _dimension_question(dim),
            turns,
            build_prompt_fn=build_prompt_fn,
            call_claude_fn=call_claude_fn,
            validate_evidence_fn=validate_evidence_fn,
        )
        result["id"] = dim.get("id")
        result["name"] = dim.get("name")
        result["weight"] = dim.get("weight") or 0
        findings.append(result)
        applog.event(
            log, "ticket_criterion_scored",
            dimension=dim.get("id"),
            verdict=result["verdict"],
            evidence_verified=result["evidence_verified"],
            evidence_seq=result["evidence_seq"],
        )
    return findings


def _numeric_score(findings: list[dict]) -> float:
    num = den = 0.0
    for f in findings:
        weight = f.get("weight") or 0
        verdict = f.get("verdict")
        if verdict in _SKIP_SCORE or weight <= 0:
            continue
        points = POINTS.get(verdict)
        if points is None:
            continue
        num += weight * points
        den += weight
    if den <= 0:
        return 0.0
    return round(100.0 * num / den, 1)


def score_ticket(
    turns: list[dict],
    dimensions: list[dict],
    *,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> dict:
    """Score a sequenced ticket against a list of {id, question, weight} dims.

    Returns score, findings (each attributed to a span's agent_user_id),
    the v1 primary_owner, and the span list itself.
    """
    spans = agent_spans(turns)
    findings = run_ticket_wave(
        turns, dimensions,
        build_prompt_fn=build_prompt_fn,
        call_claude_fn=call_claude_fn,
        validate_evidence_fn=validate_evidence_fn,
    )
    for finding in findings:
        finding["attributed_to"] = attributed_agent(
            turns, finding.get("evidence_seq"), spans,
        )
    result = {
        "score": _numeric_score(findings),
        "primary_owner": primary_owner(turns),
        "spans": spans,
        "findings": findings,
    }
    applog.event(
        log, "ticket_scored",
        score=result["score"],
        dimensions=len(findings),
        spans=len(spans),
        primary_owner=result["primary_owner"],
    )
    return result
