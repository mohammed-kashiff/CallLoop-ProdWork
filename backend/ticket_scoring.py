"""Ticket Audit Engine (TA-6, rebuilt TA-21/TA-25/TA-26): independent
per-agent scoring of a sequenced ticket.

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

TA-21 replaced v1's whole-thread-once model outright: scoring used to run
once per dimension for the whole ticket, then post-hoc attribute each
finding's evidence to whichever agent's span it happened to land in — so
if a criterion's only citable evidence sat in Agent B's turns, Agent A
got no finding at all, not a pass or fail. Every agent who touched a
ticket now gets their own independent Claude call per dimension
(`score_ticket_for_agent`), with the transcript itself marking which
turns belong to the agent under review, and with any evidence checked
to actually fall inside that agent's own span before it counts —
evidence borrowed from a teammate's turns is downgraded, never silently
accepted as that agent's own contribution. `score_ticket_per_agent` is
the entrypoint: one independent result per real, identified agent
(agent_user_id present on at least one turn) — a ticket with no
resolved agent identities yet simply has nothing to score.

Image-derived turns (TA-5) arrive as ordinary sequenced text — a vision
description injected at the right seq — so this module has no reason to know
a given turn originated from a picture. A finding that cites that seq is what
lets a reviewer open the stored screenshot next to the verdict.

internal_contribution turns (IN-9, Intercom source only) arrive the same
way — ordinary sequenced text — except a dimension flagged
customer_facing_only in ticket_rubric.py has that subset filtered out
before scoring (_scoreable_turns), since some dimensions specifically
judge what the customer actually saw. Span/ownership logic always sees
the unfiltered thread; only per-dimension scoring is affected.
"""

from __future__ import annotations

import json
import logging

from . import applog
from . import tracing
from .qa_engine import build_prompt, call_claude, validate_evidence

log = logging.getLogger("callproof.ticket_scoring")

ALLOWED_VERDICTS = ("pass", "partial", "fail")
POINTS = {"pass": 1.0, "partial": 0.5, "fail": 0.0}
_SKIP_SCORE = frozenset({"not_applicable", "error", "unverified"})


def _earned_weight(verdict: str, weight: float) -> float | None:
    """This one finding's contribution in rubric points — weight × the
    verdict's points fraction (pass=full, partial=half, fail=0) — or None
    when the finding isn't part of the weighted score at all (not_applicable/
    error/unverified, or a dimension with no weight, e.g. Response
    Timeliness). Single source of truth for the pass/partial/fail→points
    rule, shared by _numeric_score() and the per-finding "earned" field the
    frontend renders next to each criterion."""
    if verdict in _SKIP_SCORE or weight <= 0:
        return None
    points = POINTS.get(verdict)
    if points is None:
        return None
    return weight * points

_AGENT_UNDER_REVIEW = "agent under review"
_OTHER_TEAMMATE = "a different teammate — shown for context only, do not judge them"

_FOREIGN_EVIDENCE_REASONING = (
    "The model's cited evidence belonged to a different agent's turns, not this "
    "agent's own contribution — discarded rather than credited to the wrong person."
)


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


def format_turns_for_agent(turns: list[dict], target_agent_user_id: str) -> str:
    """TA-25: the same sequenced text as format_turns, except every agent
    turn is labeled with whether it belongs to the agent under review or
    a different teammate — the signal that makes independent per-agent
    scoring possible. Customer/bot turns are unchanged; the model needs
    them for context regardless of whose span they fall in."""
    lines = []
    for t in sorted(turns, key=lambda row: row["seq"]):
        text = (t.get("text") or "").replace("\r\n", "\n").replace("\n", " ").strip()
        speaker = t.get("speaker") or "unknown"
        if speaker == "agent":
            label = (
                _AGENT_UNDER_REVIEW
                if str(t.get("agent_user_id") or "") == str(target_agent_user_id)
                else _OTHER_TEAMMATE
            )
            lines.append(f'[seq {t["seq"]}] (agent — {label}) {text}')
        else:
            lines.append(f'[seq {t["seq"]}] ({speaker}) {text}')
    return "\n".join(lines)


def _question_for_agent(question: str) -> str:
    return (
        'Judge only the turns marked "(agent — agent under review)" below. '
        'Turns marked "(agent — a different teammate...)" are shown for '
        "context only — never cite them as this agent's own work, and never "
        "judge this agent on what a different teammate did. " + question
    )


def agent_spans(turns: list[dict]) -> list[dict]:
    """Split a thread into agent-owned spans.

    A span starts when an agent speaks and runs until a *different* agent
    speaks. Customer and bot turns in between stay inside the current span
    — that's the stretch of thread that agent was responsible for. Two
    agent turns with the same agent_user_id (including both NULL) merge;
    a change of agent_user_id opens a new span.

    Spans are how a finding's evidence gets checked as genuinely belonging
    to the agent it's scored for (score_ticket_for_agent), and how the UI
    highlights a viewer's own turns in the full thread (TA-30).
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


def resolved_agent_ids(turns: list[dict]) -> list[str]:
    """Every distinct, real agent_user_id with at least one agent turn on
    this ticket, in first-appearance order — the set score_ticket_per_agent
    scores independently. A turn with no resolved identity (agent_user_id
    is None — unmapped PDF name, or an unresolved Intercom identity) is
    still part of the thread everyone sees, but doesn't get its own
    scorecard: there's no real person to score."""
    seen: list[str] = []
    seen_set: set[str] = set()
    for t in sorted(turns, key=lambda row: row["seq"]):
        if t.get("speaker") != "agent":
            continue
        uid = t.get("agent_user_id")
        if uid and uid not in seen_set:
            seen_set.add(uid)
            seen.append(uid)
    return seen


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


def _seq_in_agent_span(seq: int | None, target_agent_user_id: str, spans: list[dict]) -> bool:
    if seq is None:
        return False
    return any(
        str(span.get("agent_user_id") or "") == str(target_agent_user_id)
        and span["start_seq"] <= seq <= span["end_seq"]
        for span in spans
    )


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
    transcript_text: str | None = None,
    allowed_verdicts: tuple[str, ...] = ALLOWED_VERDICTS,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> dict:
    """One question against sequenced text. Verdict + checkable quote.

    Content-blind: the same function scores a typed reply and an
    image-description turn, because both are just lines in `turns`.
    transcript_text overrides the rendered transcript sent to the model
    (score_ticket_for_agent passes format_turns_for_agent's agent-aware
    labeling) while turns stays the real, unfiltered list for evidence
    validation — validate_evidence_fn needs real seqs to check a quote
    against, regardless of how the transcript text labeled each line.
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

    text_for_model = transcript_text if transcript_text is not None else format_turns(turns)
    prompt = build_prompt_fn(question, text_for_model, list(allowed_verdicts))
    try:
        raw = call_claude_fn(prompt)
        try:
            parsed = _parse_json(raw)
        except ValueError:
            raw = call_claude_fn(
                build_prompt_fn(question, text_for_model, list(allowed_verdicts), strict=True),
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


def _scoreable_turns(turns: list[dict], dim: dict) -> list[dict]:
    """IN-9: a dimension flagged customer_facing_only (ticket_rubric.py)
    is judged — and its evidence verified — only against turns that
    aren't internal_contribution (a captured-but-not-customer-visible
    note). Filters, doesn't renumber, so evidence_seq values returned
    still index the real thread for attributed_agent()/agent_spans()
    downstream, which always see the full, unfiltered turns list."""
    if not dim.get("customer_facing_only"):
        return turns
    return [t for t in turns if not t.get("internal_contribution")]


def evaluate_criterion_for_agent(
    question: str,
    turns: list[dict],
    *,
    target_agent_user_id: str,
    spans: list[dict],
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> dict:
    """TA-25/TA-26: one criterion, independently judged for one specific
    agent. The transcript sent to the model marks which turns are this
    agent's own vs. a teammate's (format_turns_for_agent); the question
    is wrapped with an explicit instruction to judge only the marked
    agent. Evidence is then checked against real spans: if the model
    cited a turn outside this agent's own span, that's evidence for
    someone else's work, not this agent's — downgraded to "error" rather
    than silently credited or blamed to the wrong person. This is the
    fix for the root bug TA-21 exists to close: v1 scored the criterion
    once and attributed evidence after the fact, so an agent whose only
    citable moment sat in a teammate's turns got no finding at all.
    """
    result = evaluate_criterion(
        _question_for_agent(question),
        turns,
        transcript_text=format_turns_for_agent(turns, target_agent_user_id),
        build_prompt_fn=build_prompt_fn,
        call_claude_fn=call_claude_fn,
        validate_evidence_fn=validate_evidence_fn,
    )
    if result["evidence_verified"] and not _seq_in_agent_span(
        result["evidence_seq"], target_agent_user_id, spans,
    ):
        result = {
            **result,
            "verdict": "error",
            "reasoning": _FOREIGN_EVIDENCE_REASONING,
            "evidence_verified": False,
        }
    return result


def run_ticket_wave_for_agent(
    turns: list[dict],
    dimensions: list[dict],
    *,
    target_agent_user_id: str,
    spans: list[dict],
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> list[dict]:
    """Every rubric dimension, independently scored for one agent — one
    finding per dimension, always (TA-26: no dimension is ever silently
    skipped; a genuinely inapplicable one still comes back as its own
    "not_applicable" finding, never simply absent from the list)."""
    findings = []
    for dim in dimensions:
        result = evaluate_criterion_for_agent(
            _dimension_question(dim),
            _scoreable_turns(turns, dim),
            target_agent_user_id=target_agent_user_id,
            spans=spans,
            build_prompt_fn=build_prompt_fn,
            call_claude_fn=call_claude_fn,
            validate_evidence_fn=validate_evidence_fn,
        )
        weight = dim.get("weight") or 0
        result["id"] = dim.get("id")
        result["name"] = dim.get("name")
        result["weight"] = weight
        result["earned"] = _earned_weight(result["verdict"], weight)
        result["attributed_to"] = target_agent_user_id if result["evidence_verified"] else None
        findings.append(result)
        applog.event(
            log, "ticket_criterion_scored",
            dimension=dim.get("id"),
            agent_user_id=target_agent_user_id,
            verdict=result["verdict"],
            evidence_verified=result["evidence_verified"],
            evidence_seq=result["evidence_seq"],
        )
    return findings


def _numeric_score(findings: list[dict]) -> float:
    num = den = 0.0
    for f in findings:
        weight = f.get("weight") or 0
        earned = f.get("earned")
        if earned is None:
            earned = _earned_weight(f.get("verdict"), weight)
        if earned is None:
            continue
        num += earned
        den += weight
    if den <= 0:
        return 0.0
    return round(100.0 * num / den, 1)


def score_ticket_for_agent(
    turns: list[dict],
    dimensions: list[dict],
    *,
    target_agent_user_id: str,
    spans: list[dict] | None = None,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> dict:
    """One agent's complete, independent scorecard for this ticket."""
    spans = spans if spans is not None else agent_spans(turns)
    with tracing.span("task", "ticket.score_agent"):
        findings = run_ticket_wave_for_agent(
            turns, dimensions,
            target_agent_user_id=target_agent_user_id,
            spans=spans,
            build_prompt_fn=build_prompt_fn,
            call_claude_fn=call_claude_fn,
            validate_evidence_fn=validate_evidence_fn,
        )
    own_spans = [s for s in spans if str(s.get("agent_user_id") or "") == str(target_agent_user_id)]
    result = {
        "agent_user_id": target_agent_user_id,
        "score": _numeric_score(findings),
        "findings": findings,
        "spans": own_spans,
    }
    applog.event(
        log, "ticket_agent_scored",
        agent_user_id=target_agent_user_id,
        score=result["score"],
        dimensions=len(findings),
    )
    return result


def score_ticket_per_agent(
    turns: list[dict],
    dimensions: list[dict],
    *,
    only_agent_ids: list[str] | None = None,
    build_prompt_fn=build_prompt,
    call_claude_fn=call_claude,
    validate_evidence_fn=validate_evidence,
) -> list[dict]:
    """TA-21/TA-25 entrypoint: one independent scorecard per real,
    identified agent on this ticket. only_agent_ids restricts which
    agents actually get (re-)scored — the caller's rescoring guard is
    per-agent (ticket_audit_store), so a ticket that's already scored for
    Agent A but has since resolved a new Agent B only needs a real
    Claude run for B, not a full re-score of A.
    """
    spans = agent_spans(turns)
    targets = resolved_agent_ids(turns)
    if only_agent_ids is not None:
        allowed = {str(a) for a in only_agent_ids}
        targets = [a for a in targets if str(a) in allowed]
    results = [
        score_ticket_for_agent(
            turns, dimensions,
            target_agent_user_id=agent_id,
            spans=spans,
            build_prompt_fn=build_prompt_fn,
            call_claude_fn=call_claude_fn,
            validate_evidence_fn=validate_evidence_fn,
        )
        for agent_id in targets
    ]
    applog.event(
        log, "ticket_scored",
        agents=len(results),
        spans=len(spans),
    )
    return results
