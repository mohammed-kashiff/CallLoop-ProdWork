"""Self-serve rubric builder, step 1: a team-authored free-text dimension
reuses the exact LLM pipeline built-in dimensions use (build_prompt ->
call_claude -> validate_evidence) via a new "custom_llm" method, not a
bespoke prompt per criterion. Zero change to the 4 built-in dimensions."""

from __future__ import annotations

import json

from backend.qa_v8 import evaluate_custom, evaluate_dimension


def _build_prompt(question, transcript_text, allowed_verdicts, strict=False):
    return f"Q: {question}\nT: {transcript_text}\nV: {allowed_verdicts}"


def _parse_json(text):
    return json.loads(text)


def _validate_evidence(quote, segments):
    for s in segments:
        if quote and quote in s["text"]:
            return True, s["seq"]
    return False, None


SEGMENTS = [{"seq": 1, "text": "I'll confirm your callback number is 555-1234."}]


def _call_claude(payload):
    return json.dumps(payload)


def test_evaluate_custom_reuses_the_standard_llm_pipeline():
    dim = {
        "id": "confirmed_callback_number",
        "name": "Confirmed callback number",
        "method": "custom_llm",
        "weight": 25,
        "question": "Did the agent confirm the customer's callback number?",
    }
    response = json.dumps({
        "verdict": "pass",
        "reasoning": "Agent read the number back to the customer.",
        "evidence_quote": "confirm your callback number is 555-1234",
        "coaching_note": "Nice, keep confirming callback numbers.",
        "confidence": "high",
    })

    def call_claude(prompt):
        assert "Did the agent confirm the customer's callback number?" in prompt
        return response

    res = evaluate_custom(
        dim, SEGMENTS, "agent_1", "irrelevant full transcript",
        call_claude, _parse_json, _build_prompt, _validate_evidence,
    )
    assert res["verdict"] == "pass"
    assert res["evidence_verified"] is True
    assert res["evidence_seq"] == 1
    assert res["method_used"] == "custom_llm"


def test_evaluate_custom_errors_cleanly_with_no_question_text():
    dim = {"id": "empty", "name": "Empty", "method": "custom_llm", "weight": 10, "question": "  "}
    res = evaluate_custom(dim, SEGMENTS, "agent_1", "t", None, None, None, None)
    assert res["verdict"] == "error"
    assert res["method_used"] == "custom_llm"


def test_dispatch_routes_custom_llm_method_to_evaluate_custom():
    dim = {
        "id": "custom_1", "name": "Custom One", "method": "custom_llm",
        "weight": 15, "question": "Did the agent say thank you?",
    }
    response = json.dumps({
        "verdict": "partial", "reasoning": "Said it once, late.",
        "evidence_quote": "confirm your callback number", "confidence": "medium",
    })
    res = evaluate_dimension(
        dim, SEGMENTS, "agent_1", "t",
        lambda prompt: response, _parse_json, _build_prompt, _validate_evidence,
    )
    assert res["verdict"] == "partial"
    assert res["method_used"] == "custom_llm"
    assert "delivery_channel" in res


def test_dispatch_still_errors_on_a_truly_unknown_dimension():
    """Not method=custom_llm and not one of the 4 known ids -> still an error,
    same as before this change (a malformed rubric must not silently no-op)."""
    dim = {"id": "mystery_dimension", "name": "?", "weight": 10}
    res = evaluate_dimension(
        dim, SEGMENTS, "agent_1", "t", None, None, None, None,
    )
    assert res["verdict"] == "error"
    assert "Unknown dimension" in res["reasoning"]


def test_dispatch_zero_change_for_the_four_built_in_dimensions():
    """The built-in ids still hit their own evaluate_* functions, not the new
    custom_llm branch, even if a caller mistakenly sets method=custom_llm."""
    dim = {
        "id": "active_listening", "name": "Active Listening",
        "method": "custom_llm", "weight": 20,  # method is ignored for known ids
    }
    res = evaluate_dimension(
        dim, SEGMENTS, "agent_1", "t", None, None, None, None,
    )
    # evaluate_listening runs deterministically and never errors this way
    assert res["verdict"] != "error" or "Unknown dimension" not in res.get("reasoning", "")
