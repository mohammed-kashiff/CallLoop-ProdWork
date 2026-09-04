"""
CallProof v8 rubric evaluation (dimensions + conditional LLM steps).

Works with the runtime rubric.json (v8 shape) and rules_v8.py. Keeps numeric 0-100 scoring and
maps it to v8 performance bands (Star Performer … Needs Immediate Attention).
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import applog
from . import rules_v8 as rv8

log = logging.getLogger("callproof.qa.v8")

RESOLUTION_QUESTION = (
    "Evaluate RESOLUTION EFFECTIVENESS for the AGENT only. "
    "Did the agent resolve the customer's issue, or make clear, appropriate "
    "progress (including a justified escalation)? "
    "pass = issue resolved or clearly progressed with a sound plan; "
    "partial = some effort but incomplete / unclear progress; "
    "fail = no meaningful resolution path. "
    "Cite one exact transcript quote."
)

TONE_QUESTION = (
    "Evaluate the AGENT's tone, empathy, and professionalism across the call. "
    "pass = consistently warm/professional; "
    "partial = mostly fine with a clear dip; "
    "fail = cold, dismissive, or unprofessional (without requiring swear words). "
    "Cite one exact transcript quote."
)

OWNERSHIP_STEP2_TEMPLATE = (
    "The agent used vague language about next steps ('{matched_term}'). "
    "Considering the full context of this line and surrounding turns: is the agent "
    "being transparently honest about a genuine constraint outside their control "
    "while still taking personal accountability, or being dismissively vague to "
    "avoid commitment? "
    'Return ONLY JSON: {{"classification": "transparent_honest" | "dismissive", '
    '"reasoning": "...", "coaching_note": "1-2 sentences to the agent", '
    '"confidence": "high" | "medium" | "low"}}'
)


def is_v8_rubric(rubric: dict) -> bool:
    return "technical_skills" in rubric and "soft_skills" in rubric


def list_dimensions(rubric: dict) -> list[dict]:
    dims = []
    for bucket in ("technical_skills", "soft_skills"):
        block = rubric.get(bucket) or {}
        for d in block.get("dimensions") or []:
            dims.append(dict(d))
    return dims


def performance_band(score, rubric: dict | None = None) -> str:
    """Numeric score stays primary; band is a display label from the rubric."""
    bands = ((rubric or {}).get("score_bands") or {}).get("bands") or []
    if bands:
        for b in bands:
            try:
                if b["min"] <= score <= b["max"]:
                    return b["label"]
            except (KeyError, TypeError):
                continue
    return rv8.score_band(score)


def _llm_json(call_claude, parse_json, prompt: str) -> dict:
    raw = call_claude(prompt)
    try:
        return parse_json(raw)
    except Exception:
        # one aimed retry
        raw2 = call_claude(prompt + "\n\nPrevious reply was not valid JSON. Return ONLY JSON.")
        return parse_json(raw2)


def _run_standard_llm(call_claude, parse_json, build_prompt, validate_evidence,
                      question, transcript_text, segments, coaching_extra=""):
    q = question
    if coaching_extra:
        q = f"{question}\n\nAlso: {coaching_extra}"
    prompt = build_prompt(q, transcript_text, ["pass", "partial", "fail"])
    # Extend schema request for coaching_note + confidence
    prompt += (
        '\nAlso include: "coaching_note": "1-2 sentences to the agent", '
        '"confidence": "high" | "medium" | "low".'
    )
    try:
        parsed = _llm_json(call_claude, parse_json, prompt)
    except Exception as e:  # noqa: BLE001
        err = applog.safe_exception_text(e)
        log.error("v8 LLM step failed: %s", err)
        return {
            "verdict": "error",
            "reasoning": f"LLM step failed: {err}",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
            "coaching_note": None,
        }

    quote = parsed.get("evidence_quote") or parsed.get("evidence_text") or ""
    verified, seq = validate_evidence(quote, segments)
    note = parsed.get("coaching_note")
    if note:
        note, _flag = rv8.safe_coaching_note(note, quote, transcript_text)
    return {
        "verdict": parsed.get("verdict", "error"),
        "reasoning": parsed.get("reasoning", ""),
        "evidence_text": quote or None,
        "evidence_seq": seq if verified else parsed.get("evidence_seq"),
        "evidence_verified": verified,
        "coaching_note": note,
        "confidence": parsed.get("confidence"),
    }


def evaluate_resolution(dim, segments, agent, transcript_text, call_claude, parse_json,
                        build_prompt, validate_evidence):
    extra = ((dim.get("coaching_output") or {}).get("prompt_addition") or "")
    return _run_standard_llm(
        call_claude, parse_json, build_prompt, validate_evidence,
        RESOLUTION_QUESTION, transcript_text, segments, extra,
    )


def evaluate_ownership(dim, segments, agent, transcript_text, call_claude, parse_json,
                       llm_enabled=True):
    step1 = rv8.check_ownership_step1(segments, agent)
    if not step1.get("needs_llm"):
        return {
            "verdict": step1["verdict"],
            "reasoning": step1["reasoning"],
            "evidence_text": step1.get("evidence_text"),
            "evidence_seq": step1.get("evidence_seq"),
            "evidence_verified": step1.get("evidence_text") is not None,
            "coaching_note": step1.get("coaching_note"),
            "hostile_override": False,
            "method_used": "deterministic",
        }

    if not llm_enabled:
        term = (step1.get("llm_context") or {}).get("matched_term", "")
        combined = rv8.combine_ownership_result(step1, "dismissive", transcript_text)
        combined["reasoning"] = (
            f"Partial because the closing used vague language ('{term}') instead of a personal "
            f"commitment. Hybrid rules do not ask Claude whether that was honest or dismissive, "
            f"so vague language scores half credit (10 of 20)."
        )
        return {
            "verdict": combined["verdict"],
            "reasoning": combined["reasoning"],
            "evidence_text": combined.get("evidence_text"),
            "evidence_seq": combined.get("evidence_seq"),
            "evidence_verified": combined.get("evidence_text") is not None,
            "coaching_note": combined.get("coaching_note"),
            "hostile_override": False,
            "method_used": "deterministic_hybrid",
        }

    term = (step1.get("llm_context") or {}).get("matched_term", "")
    prompt = OWNERSHIP_STEP2_TEMPLATE.format(matched_term=term)
    prompt = (
        f"{prompt}\n\nTRANSCRIPT:\n{transcript_text}\n"
        f"Focus near seq {(step1.get('llm_context') or {}).get('segment_seq')}."
    )
    try:
        parsed = _llm_json(call_claude, parse_json, prompt)
        classification = parsed.get("classification", "dismissive")
        if classification not in ("transparent_honest", "dismissive"):
            classification = "dismissive"
    except Exception as e:  # noqa: BLE001
        err = applog.safe_exception_text(e)
        log.error("ownership step2 failed: %s", err)
        classification = "dismissive"
        parsed = {"reasoning": err, "coaching_note": None}

    combined = rv8.combine_ownership_result(step1, classification, transcript_text)
    # Prefer model coaching note when present and safe
    if parsed.get("coaching_note") and classification == "dismissive":
        note, _f = rv8.safe_coaching_note(
            parsed["coaching_note"], step1.get("evidence_text"), transcript_text,
            "ownership_next_steps",
        )
        combined["coaching_note"] = note
    return {
        "verdict": combined["verdict"],
        "reasoning": combined["reasoning"],
        "evidence_text": combined.get("evidence_text"),
        "evidence_seq": combined.get("evidence_seq"),
        "evidence_verified": combined.get("evidence_text") is not None,
        "coaching_note": combined.get("coaching_note"),
        "hostile_override": False,
        "method_used": "deterministic_plus_llm",
    }


def evaluate_listening(dim, segments, agent, transcript_text, resolution_passed=False):
    r = rv8.score_listening_categories(
        segments, agent, resolution_passed=resolution_passed,
    )
    return {
        "verdict": r["verdict"],
        "reasoning": r["reasoning"],
        "evidence_text": r.get("evidence_text"),
        "evidence_seq": r.get("evidence_seq"),
        "evidence_verified": r.get("evidence_text") is not None,
        "coaching_note": r.get("coaching_note"),
        "hostile_override": False,
        "method_used": "deterministic",
        "checks": r.get("checks") or [],
    }


def evaluate_tone(dim, segments, agent, transcript_text, call_claude, parse_json,
                  build_prompt, validate_evidence, llm_enabled=True,
                  resolution_passed=False):
    scored = rv8.score_tone_categories(
        segments, agent, resolution_passed=resolution_passed,
    )
    checks = scored.get("checks") or []

    if scored.get("hostile_override"):
        return {
            "verdict": "fail",
            "reasoning": scored["reasoning"],
            "evidence_text": scored.get("evidence_text"),
            "evidence_seq": scored.get("evidence_seq"),
            "evidence_verified": scored.get("evidence_text") is not None,
            "coaching_note": None,
            "hostile_override": True,
            "method_used": "deterministic",
            "checks": checks,
        }

    if not llm_enabled:
        return {
            "verdict": scored["verdict"],
            "reasoning": scored["reasoning"],
            "evidence_text": scored.get("evidence_text"),
            "evidence_seq": scored.get("evidence_seq"),
            "evidence_verified": scored.get("evidence_text") is not None,
            "coaching_note": None,
            "hostile_override": False,
            "method_used": "deterministic_hybrid",
            "checks": checks,
        }

    extra = ""
    for step in dim.get("evaluation_order") or []:
        if step.get("step") == 2:
            extra = ((step.get("coaching_output") or {}).get("prompt_addition") or "")
    llm = _run_standard_llm(
        call_claude, parse_json, build_prompt, validate_evidence,
        TONE_QUESTION, transcript_text, segments, extra,
    )
    combined = rv8.combine_tone_result(
        {"needs_llm": True},
        llm_verdict=llm.get("verdict"),
        llm_reasoning=llm.get("reasoning"),
        llm_evidence_seq=llm.get("evidence_seq"),
        llm_evidence_text=llm.get("evidence_text"),
        llm_coaching_note=llm.get("coaching_note"),
        full_transcript_text=transcript_text,
    )
    return {
        "verdict": combined["verdict"],
        "reasoning": combined["reasoning"],
        "evidence_text": combined.get("evidence_text"),
        "evidence_seq": combined.get("evidence_seq"),
        "evidence_verified": combined.get("evidence_text") is not None,
        "coaching_note": combined.get("coaching_note"),
        "hostile_override": False,
        "confidence": llm.get("confidence"),
        "method_used": "deterministic_override_plus_llm",
        "checks": checks,
    }


def evaluate_custom(dim, segments, agent, transcript_text, call_claude, parse_json,
                    build_prompt, validate_evidence):
    """A team-authored, free-text-judged dimension (self-serve rubric builder).

    Reuses the exact pipeline the built-in LLM dimensions use (build_prompt ->
    call_claude -> validate_evidence) — no bespoke prompt engineering needed
    per custom criterion, the same mechanism that already powers Resolution
    Effectiveness just pointed at the team's own question text.
    """
    question = (dim.get("question") or "").strip()
    if not question:
        return {
            "verdict": "error",
            "reasoning": "This custom dimension has no criteria text.",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
            "coaching_note": None,
            "method_used": "custom_llm",
        }
    res = _run_standard_llm(
        call_claude, parse_json, build_prompt, validate_evidence,
        question, transcript_text, segments,
    )
    res.setdefault("method_used", "custom_llm")
    return res


def evaluate_dimension(dim, segments, agent, transcript_text, call_claude, parse_json,
                       build_prompt, validate_evidence, llm_enabled=True):
    did = dim["id"]
    if did == "resolution_effectiveness":
        res = evaluate_resolution(
            dim, segments, agent, transcript_text, call_claude, parse_json,
            build_prompt, validate_evidence,
        )
        res.setdefault("method_used", "llm")
    elif did == "ownership_next_steps":
        res = evaluate_ownership(
            dim, segments, agent, transcript_text, call_claude, parse_json,
            llm_enabled=llm_enabled,
        )
    elif did == "active_listening":
        res = evaluate_listening(dim, segments, agent, transcript_text)
    elif did == "tone_empathy_professionalism":
        res = evaluate_tone(
            dim, segments, agent, transcript_text, call_claude, parse_json,
            build_prompt, validate_evidence,
            llm_enabled=llm_enabled,
        )
    elif dim.get("method") == "custom_llm":
        res = evaluate_custom(
            dim, segments, agent, transcript_text, call_claude, parse_json,
            build_prompt, validate_evidence,
        )
    else:
        res = {
            "verdict": "error",
            "reasoning": f"Unknown dimension '{did}'",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
            "coaching_note": None,
        }
    res["delivery_channel"] = rv8.coaching_delivery_channel(res.get("verdict"))
    return res


def score_v8(results):
    """results: list of (dimension_dict, result_dict). Returns score, tally, hostile."""
    verdicts = {}
    tally = {}
    hostile = False
    weights = {}
    for dim, res in results:
        v = res.get("verdict") or "error"
        tally[v] = tally.get(v, 0) + 1
        if v in ("pass", "partial", "fail"):
            verdicts[dim["id"]] = v
        else:
            verdicts[dim["id"]] = "fail"
        if res.get("hostile_override"):
            hostile = True
        weights[dim["id"]] = dim["weight"]
    # Fill missing dimensions as fail so aggregate_score always has all keys
    for dim_id in weights:
        verdicts.setdefault(dim_id, "fail")
    score = rv8.aggregate_score(verdicts, tone_hostile_override=hostile, weights=weights)
    return score, tally, hostile


def run_v8_wave(rubric, segments, agent_speaker, transcript_text,
                call_claude, parse_json, build_prompt, validate_evidence,
                assess_churn, _extract_feedback=None,
                max_workers=None, audit_mode="full", on_dimension_event=None):
    """on_dimension_event(dim, status, detail), status in
    'started'/'succeeded'/'failed' — optional, dependency-injected the same
    way call_claude/parse_json/build_prompt already are, so this module
    stays decoupled from whatever records it (call_trail, in api.py).
    Never allowed to break scoring: any exception it raises is swallowed."""
    dims = list_dimensions(rubric)
    n = len(dims)
    hybrid = audit_mode == "hybrid"
    llm_enabled = not hybrid
    # hybrid: extra worker for dedicated churn LLM (parallel with resolution).
    workers = max_workers or min(16, max(4, n + 1))
    ordered = [None] * n
    churn = None
    t0 = time.perf_counter()

    def _notify(dim, status, detail=None):
        if on_dimension_event is None:
            return
        try:
            on_dimension_event(dim, status, detail)
        except Exception:  # noqa: BLE001
            pass

    def _eval(dim):
        _notify(dim, "started", {
            "method": dim.get("method"), "weight": dim.get("weight"),
            "name": dim.get("name"),
        })
        try:
            res = evaluate_dimension(
                dim, segments, agent_speaker, transcript_text,
                call_claude, parse_json, build_prompt, validate_evidence,
                llm_enabled=llm_enabled,
            )
        except Exception as e:
            _notify(dim, "failed", {"error": str(e)})
            raise
        verdict = res.get("verdict")
        _notify(
            dim, "failed" if verdict == "error" else "succeeded",
            {"verdict": verdict, "reasoning": res.get("reasoning")},
        )
        return res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for i, dim in enumerate(dims):
            futs[pool.submit(_eval, dim)] = ("dim", i, dim)
        futs[pool.submit(assess_churn, transcript_text, segments)] = ("churn", None, None)
        for fut in as_completed(futs):
            kind, i, dim = futs[fut]
            if kind == "dim":
                ordered[i] = (dim, fut.result())
            else:
                churn = fut.result()

    # Agent/product areas of improvement are on-demand (button), not first load.
    feedback = {
        "status": "skipped",
        "reason": "on_demand",
        "agent": [],
        "product": [],
    }

    resolution_passed = False
    for item in ordered:
        if not item:
            continue
        dim, res = item
        if dim.get("id") == "resolution_effectiveness" and res.get("verdict") == "pass":
            resolution_passed = True
            break

    if resolution_passed:
        for i, item in enumerate(ordered):
            if not item:
                continue
            dim, res = item
            did = dim.get("id")
            if did == "active_listening":
                new_res = evaluate_listening(
                    dim, segments, agent_speaker, transcript_text,
                    resolution_passed=True,
                )
                new_res["delivery_channel"] = rv8.coaching_delivery_channel(
                    new_res.get("verdict")
                )
                ordered[i] = (dim, new_res)
            elif did == "tone_empathy_professionalism" and not res.get("hostile_override"):
                if hybrid:
                    new_res = evaluate_tone(
                        dim, segments, agent_speaker, transcript_text,
                        call_claude, parse_json, build_prompt, validate_evidence,
                        llm_enabled=False,
                        resolution_passed=True,
                    )
                    new_res["delivery_channel"] = rv8.coaching_delivery_channel(
                        new_res.get("verdict")
                    )
                    ordered[i] = (dim, new_res)
                else:
                    scored = rv8.score_tone_categories(
                        segments, agent_speaker, resolution_passed=True,
                    )
                    patched = dict(res)
                    patched["checks"] = scored.get("checks") or []
                    ordered[i] = (dim, patched)

    score, tally, hostile = score_v8(ordered)
    grade = performance_band(score, rubric)

    dim_for_review = []
    for dim, res in ordered:
        pts = rv8.WEIGHTS.get(dim["id"], dim.get("weight", 0)) * rv8.VERDICT_POINTS.get(
            res.get("verdict"), 0.0
        )
        dim_for_review.append({
            "id": dim["id"],
            "score": pts,
            "evidence_seq": res.get("evidence_seq"),
            "evidence_text": res.get("evidence_text"),
        })
    hostile_ev = None
    for dim, res in ordered:
        if res.get("hostile_override"):
            hostile_ev = {
                "evidence_seq": res.get("evidence_seq"),
                "evidence_text": res.get("evidence_text"),
            }
            break
    manager_review = rv8.check_manager_review(
        dim_for_review, score, hostile, hostile_ev,
    )

    log.info(
        "v8 wave done in %.1fs (%d dims) mode=%s score=%s band=%s hostile=%s review=%s",
        time.perf_counter() - t0, n, audit_mode, score, grade, hostile, len(manager_review),
    )
    # Retention email and areas of improvement are on-demand (not in this wave).
    return ordered, churn, feedback, None, score, tally, grade, hostile, manager_review


def _why_this_score(dim, res) -> str:
    """Plain-language explanation of verdict + points for the UI."""
    weight = dim.get("weight") or rv8.WEIGHTS.get(dim["id"], 0)
    v = res.get("verdict") or "error"
    frac = rv8.VERDICT_POINTS.get(v)
    name = dim.get("name") or dim["id"]
    reason = (res.get("reasoning") or "").strip()
    labels = {"pass": "Pass", "partial": "Partial", "fail": "Fail"}
    if frac is None:
        head = f"{name} was not scored ({v})."
    else:
        pts = round(weight * frac, 1)
        head = f"{name}: {labels.get(v, v)} — {pts} of {weight} points."
    if not reason:
        return head
    if reason.lower().startswith(("pass because", "partial because", "fail because")):
        return f"{head} {reason}"
    return f"{head} {reason}"


def findings_from_v8(results):
    out = []
    for dim, res in results:
        weight = dim.get("weight") or rv8.WEIGHTS.get(dim["id"], 0)
        v = res.get("verdict")
        pts = None
        if v in rv8.VERDICT_POINTS:
            pts = round(weight * rv8.VERDICT_POINTS[v], 1)
        out.append({
            "id": dim["id"],
            "name": dim["name"],
            "method": res.get("method_used") or dim.get("method"),
            "weight": weight,
            "is_gate": bool(res.get("hostile_override")),
            "verdict": v,
            "reasoning": res.get("reasoning", ""),
            "why": _why_this_score(dim, res),
            "points": pts,
            "subchecks": res.get("checks") or [],
            "evidence_text": res.get("evidence_text"),
            "evidence_seq": res.get("evidence_seq"),
            "evidence_verified": res.get("evidence_verified"),
            "coaching_note": res.get("coaching_note"),
            "delivery_channel": res.get("delivery_channel"),
        })
    return out


def coaching_from_v8(results):
    tips = []
    for dim, res in results:
        note = res.get("coaching_note")
        if not note:
            continue
        tips.append({
            "criterion": dim["name"],
            "tip": note,
            "delivery_channel": res.get("delivery_channel") or rv8.coaching_delivery_channel(res.get("verdict")),
        })
    return tips
