"""TA-7: ticket-native scaffold rubric — exists only to exercise TA-6
end to end, not a finished design (PRD §4/§10/§11). Every dimension must
carry scaffold=True so nothing downstream can mistake it for the real,
future TA-13 rubric."""

from __future__ import annotations

import ast
import json

from backend.paths import ROOT
from backend.ticket_rubric import SCAFFOLD_TICKET_RUBRIC, get_scaffold_rubric

FORBIDDEN_MODULES = ("rules_v8", "qa_v8")


def test_shares_no_code_with_the_call_rubric_dispatch():
    src = (ROOT / "backend" / "ticket_rubric.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in FORBIDDEN_MODULES
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not any(name in mod.split(".") for name in FORBIDDEN_MODULES)
    # no rules_v8-style deterministic "method" dispatch on any dimension
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert "method" not in dim


def test_every_dimension_is_labeled_as_scaffold():
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert dim["scaffold"] is True, dim["id"]


def test_every_dimension_has_the_fields_ticket_scoring_reads():
    for dim in SCAFFOLD_TICKET_RUBRIC:
        assert isinstance(dim["id"], str) and dim["id"]
        assert isinstance(dim["name"], str) and dim["name"]
        assert isinstance(dim["weight"], (int, float)) and dim["weight"] > 0
        assert isinstance(dim["question"], str) and dim["question"].strip()


def test_dimension_ids_are_unique():
    ids = [d["id"] for d in SCAFFOLD_TICKET_RUBRIC]
    assert len(ids) == len(set(ids))


def test_weights_sum_to_one_hundred():
    assert sum(d["weight"] for d in SCAFFOLD_TICKET_RUBRIC) == 100


def test_covers_the_six_prd_named_criteria():
    ids = {d["id"] for d in SCAFFOLD_TICKET_RUBRIC}
    assert ids == {
        "tone", "ownership", "diagnostic_reasoning", "investigation_rigor",
        "resolution_effectiveness", "escalation_quality",
    }


def test_get_scaffold_rubric_returns_an_independent_copy():
    a = get_scaffold_rubric()
    a[0]["weight"] = 999
    b = get_scaffold_rubric()
    assert b[0]["weight"] != 999
    assert b == SCAFFOLD_TICKET_RUBRIC


# ---------- exercises TA-6 end to end with this real rubric shape ----------


TURNS = [
    {"seq": 0, "speaker": "customer", "agent_user_id": None,
     "text": "Checkout keeps failing with a 504 error."},
    {"seq": 1, "speaker": "agent", "agent_user_id": None,
     "text": "I can see the 504 in the logs — restarting the payment worker now."},
    {"seq": 2, "speaker": "customer", "agent_user_id": None,
     "text": "That fixed it, thank you!"},
]


def test_scaffold_rubric_runs_end_to_end_through_ticket_scoring():
    from backend import ticket_scoring

    def _dispatch(prompt: str) -> str:
        if "diagnose" in prompt.lower():
            return json.dumps({
                "verdict": "pass", "reasoning": "correctly named the 504",
                "evidence_quote": "I can see the 504 in the logs", "evidence_seq": 1,
            })
        if "resolved" in prompt.lower():
            return json.dumps({
                "verdict": "pass", "reasoning": "customer confirmed the fix",
                "evidence_quote": "That fixed it, thank you!", "evidence_seq": 2,
            })
        return json.dumps({
            "verdict": "not_applicable", "reasoning": "n/a for this thread",
            "evidence_quote": "", "evidence_seq": None,
        })

    result = ticket_scoring.score_ticket(
        TURNS, get_scaffold_rubric(), call_claude_fn=_dispatch,
    )
    assert 0 <= result["score"] <= 100
    by_id = {f["id"]: f for f in result["findings"]}
    assert set(by_id) == {d["id"] for d in SCAFFOLD_TICKET_RUBRIC}
    assert by_id["diagnostic_reasoning"]["verdict"] == "pass"
    assert by_id["diagnostic_reasoning"]["evidence_verified"] is True
    assert by_id["resolution_effectiveness"]["verdict"] == "pass"
    # escalation/ownership/tone/investigation weren't matched by the fake
    # dispatcher's keywords, so they legitimately came back not_applicable
    # — not_applicable dimensions are excluded from the score, not scored
    # as failures (see ticket_scoring._numeric_score / _SKIP_SCORE).
    assert by_id["escalation_quality"]["verdict"] == "not_applicable"
