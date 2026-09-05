"""TA-6: ticket scoring is a separate engine sharing only three primitives.

Guardrail (PRD §13): this module must not import anything from qa_engine.py
other than build_prompt / call_claude / validate_evidence, and must not
import qa_v8.py or rules_v8.py at all. The call-engine files themselves
are not touched; that's asserted by the absence of edits, plus the AST
check below so a later import can't silently couple the two engines.
"""

from __future__ import annotations

import ast
import json

from backend.paths import ROOT

CALL_ENGINE = ("qa_engine", "qa_v8", "rules_v8")
ALLOWED_FROM_QA_ENGINE = {"build_prompt", "call_claude", "validate_evidence"}


TURNS = [
    {"seq": 0, "speaker": "customer", "agent_user_id": None,
     "text": "The checkout returns error code 504 every time I submit."},
    {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a",
     "text": "I can help with that. Let me look at the 504 on checkout."},
    {"seq": 2, "speaker": "customer", "agent_user_id": None,
     "text": "Still failing. Screenshot of the error dialog is above."},
    {"seq": 3, "speaker": "agent", "agent_user_id": "agent-b",
     "text": "I've restarted the payment worker and the 504 is gone now."},
]


def _claude_for(quote: str, seq: int, verdict: str = "pass"):
    def _fn(prompt: str) -> str:
        return json.dumps({
            "verdict": verdict,
            "reasoning": "The sequenced text supports this verdict.",
            "evidence_quote": quote,
            "evidence_seq": seq,
        })
    return _fn


def test_ticket_scoring_imports_only_the_three_shared_primitives():
    src = (ROOT / "backend" / "ticket_scoring.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    seen_from_qa_engine = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in CALL_ENGINE, alias.name
                assert "qa_v8" not in alias.name
                assert "rules_v8" not in alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            bits = {p for p in mod.split(".") if p}
            forbidden = bits & set(CALL_ENGINE)
            if not forbidden:
                continue
            assert forbidden == {"qa_engine"}, f"ticket_scoring imported {mod}"
            names = {a.name for a in node.names}
            assert names <= ALLOWED_FROM_QA_ENGINE, names
            seen_from_qa_engine |= names
    assert seen_from_qa_engine == ALLOWED_FROM_QA_ENGINE


def test_call_engine_files_are_untouched_by_this_module():
    """TA-6's binary guardrail: those three files must not be edited for
    the ticket engine. We only assert the ticket module doesn't reach
    into them beyond the three named primitives — the files' contents
    are the call engine's."""
    for name in ("qa_engine.py", "qa_v8.py", "rules_v8.py"):
        src = (ROOT / "backend" / name).read_text(encoding="utf-8")
        assert "ticket_scoring" not in src
        assert "run_ticket_wave" not in src


def test_evaluate_criterion_is_content_blind_question_plus_turns():
    from backend.ticket_scoring import evaluate_criterion

    result = evaluate_criterion(
        "Did the agent acknowledge the error code?",
        TURNS,
        call_claude_fn=_claude_for("Let me look at the 504 on checkout", 1),
    )
    assert result["verdict"] == "pass"
    assert result["evidence_verified"] is True
    assert result["evidence_seq"] == 1
    assert result["evidence_text"] == "Let me look at the 504 on checkout"


def test_image_description_turn_is_just_another_line_of_sequenced_text():
    from backend.ticket_scoring import evaluate_criterion

    turns = [
        TURNS[0], TURNS[1],
        {"seq": 2, "speaker": "customer", "agent_user_id": None,
         "text": "Screenshot: error dialog reading Connection timeout, error code 504."},
        TURNS[3],
    ]
    quote = "error dialog reading Connection timeout, error code 504"
    result = evaluate_criterion(
        "Did the agent use the available technical information?",
        turns,
        call_claude_fn=_claude_for(quote, 2),
    )
    assert result["evidence_verified"] is True
    assert result["evidence_seq"] == 2


def test_unverified_quote_keeps_claimed_seq_but_marks_unverified():
    from backend.ticket_scoring import evaluate_criterion

    result = evaluate_criterion(
        "Did the agent resolve the issue?",
        TURNS,
        call_claude_fn=_claude_for("this quote is not in the thread", 3),
    )
    assert result["evidence_verified"] is False
    assert result["evidence_seq"] == 3
    assert result["verdict"] == "pass"


def test_empty_question_does_not_call_claude():
    from backend.ticket_scoring import evaluate_criterion

    def _boom(_prompt):
        raise AssertionError("Claude must not be called for an empty question")

    result = evaluate_criterion("  ", TURNS, call_claude_fn=_boom)
    assert result["verdict"] == "error"
    assert result["evidence_verified"] is False


def test_agent_spans_split_when_a_different_agent_picks_up():
    from backend.ticket_scoring import agent_spans

    spans = agent_spans(TURNS)
    assert len(spans) == 2
    assert spans[0] == {
        "agent_user_id": "agent-a",
        "start_seq": 1,
        "end_seq": 2,  # customer reply stays in agent-a's span
        "turn_count": 1,
    }
    assert spans[1] == {
        "agent_user_id": "agent-b",
        "start_seq": 3,
        "end_seq": 3,
        "turn_count": 1,
    }


def test_agent_spans_merge_consecutive_turns_from_the_same_agent():
    from backend.ticket_scoring import agent_spans

    turns = [
        {"seq": 0, "speaker": "customer", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a", "text": "one"},
        {"seq": 2, "speaker": "customer", "agent_user_id": None, "text": "ok"},
        {"seq": 3, "speaker": "agent", "agent_user_id": "agent-a", "text": "two"},
    ]
    spans = agent_spans(turns)
    assert len(spans) == 1
    assert spans[0]["start_seq"] == 1
    assert spans[0]["end_seq"] == 3
    assert spans[0]["turn_count"] == 2


def test_agent_spans_treat_null_agent_user_id_as_one_identity():
    """PDF MVP cannot resolve org_members.user_id; two unnamed agents
    collapse into one span. That's the known TA-2/TA-3 limitation, not a
    bug in the span splitter."""
    from backend.ticket_scoring import agent_spans

    turns = [
        {"seq": 0, "speaker": "agent", "agent_user_id": None, "text": "a"},
        {"seq": 1, "speaker": "customer", "agent_user_id": None, "text": "b"},
        {"seq": 2, "speaker": "agent", "agent_user_id": None, "text": "c"},
    ]
    spans = agent_spans(turns)
    assert len(spans) == 1
    assert spans[0]["agent_user_id"] is None
    assert spans[0]["turn_count"] == 2


def test_primary_owner_is_the_resolving_agent_when_that_turn_has_an_id():
    from backend.ticket_scoring import primary_owner

    assert primary_owner(TURNS) == "agent-b"


def test_primary_owner_falls_back_to_most_turns_when_resolver_has_no_id():
    from backend.ticket_scoring import primary_owner

    turns = [
        {"seq": 0, "speaker": "agent", "agent_user_id": "agent-a", "text": "a1"},
        {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a", "text": "a2"},
        {"seq": 2, "speaker": "agent", "agent_user_id": None, "text": "closing"},
    ]
    assert primary_owner(turns) == "agent-a"


def test_primary_owner_is_none_when_every_agent_turn_is_unresolved():
    from backend.ticket_scoring import primary_owner

    turns = [
        {"seq": 0, "speaker": "customer", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "agent_user_id": None, "text": "hello"},
    ]
    assert primary_owner(turns) is None


def test_finding_is_attributed_to_the_span_that_owns_the_evidence_seq():
    from backend.ticket_scoring import score_ticket

    dims = [
        {"id": "resolution", "name": "Resolution", "weight": 50,
         "question": "Was the issue resolved?"},
        {"id": "tone", "name": "Tone", "weight": 50,
         "question": "Was the agent professional?"},
    ]
    calls = {
        "Was the issue resolved?": _claude_for(
            "I've restarted the payment worker and the 504 is gone now", 3,
        ),
        "Was the agent professional?": _claude_for(
            "I can help with that", 1,
        ),
    }

    def _dispatch(prompt: str) -> str:
        for question, fn in calls.items():
            if question in prompt:
                return fn(prompt)
        raise AssertionError(f"unexpected prompt: {prompt[:80]}")

    result = score_ticket(TURNS, dims, call_claude_fn=_dispatch)
    assert result["primary_owner"] == "agent-b"
    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["resolution"]["attributed_to"] == "agent-b"
    assert by_id["tone"]["attributed_to"] == "agent-a"
    assert by_id["resolution"]["evidence_verified"] is True
    assert result["score"] == 100.0


def test_score_renormalises_when_a_dimension_errors():
    from backend.ticket_scoring import score_ticket

    dims = [
        {"id": "ok", "question": "Q1", "weight": 40},
        {"id": "bad", "question": "Q2", "weight": 60},
    ]

    def _dispatch(prompt: str) -> str:
        if "Q1" in prompt:
            return json.dumps({
                "verdict": "pass",
                "reasoning": "ok",
                "evidence_quote": "I can help with that",
                "evidence_seq": 1,
            })
        return "not json at all"

    result = score_ticket(TURNS, dims, call_claude_fn=_dispatch)
    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["bad"]["verdict"] == "error"
    assert result["score"] == 100.0  # only the passing 40-weight dim counts


def test_score_ticket_does_not_call_run_v8_wave(monkeypatch):
    """Own loop: even if qa_v8.run_v8_wave exists, TA-6 must not touch it."""
    from backend import ticket_scoring

    def _forbidden(*_a, **_k):
        raise AssertionError("run_v8_wave must not be used by the ticket engine")

    monkeypatch.setattr("backend.qa_v8.run_v8_wave", _forbidden, raising=False)
    dims = [{"id": "x", "question": "Did the agent help?", "weight": 10}]
    result = ticket_scoring.score_ticket(
        TURNS, dims,
        call_claude_fn=_claude_for("I can help with that", 1),
    )
    assert result["findings"][0]["verdict"] == "pass"
