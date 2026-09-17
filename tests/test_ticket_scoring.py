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


# ── _scoreable_turns / run_ticket_wave customer_facing_only (IN-9) ───────────


TURNS_WITH_A_NOTE = [
    TURNS[0],
    TURNS[1],
    {"seq": 2, "speaker": "agent", "agent_user_id": "agent-a",
     "text": "Refund approved per policy, internal note only.", "internal_contribution": True},
    TURNS[3],
]


def test_scoreable_turns_returns_everything_when_flag_is_absent():
    from backend.ticket_scoring import _scoreable_turns

    assert _scoreable_turns(TURNS_WITH_A_NOTE, {"id": "ownership"}) == TURNS_WITH_A_NOTE


def test_scoreable_turns_excludes_internal_when_customer_facing_only():
    from backend.ticket_scoring import _scoreable_turns

    result = _scoreable_turns(TURNS_WITH_A_NOTE, {"id": "tone", "customer_facing_only": True})
    assert result == [TURNS[0], TURNS[1], TURNS[3]]
    assert all(not t.get("internal_contribution") for t in result)


def test_run_ticket_wave_excludes_a_note_from_a_customer_facing_only_dimension():
    """The note's own text must never reach Claude's prompt for a
    customer_facing_only dimension — proven by having the stub Claude
    fail the test if the note's text is anywhere in what it receives."""
    from backend.ticket_scoring import agent_spans, run_ticket_wave_for_agent

    captured_prompts = []

    def _claude(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps({
            "verdict": "pass", "reasoning": "ok",
            "evidence_quote": "I can help with that.", "evidence_seq": 1,
        })

    dims = [{"id": "tone", "name": "Tone", "weight": 15,
             "question": "Was the tone professional?", "customer_facing_only": True}]
    run_ticket_wave_for_agent(
        TURNS_WITH_A_NOTE, dims,
        target_agent_user_id="agent-a", spans=agent_spans(TURNS_WITH_A_NOTE),
        call_claude_fn=_claude,
    )
    assert len(captured_prompts) == 1
    assert "Refund approved per policy" not in captured_prompts[0]


def test_run_ticket_wave_includes_a_note_for_a_dimension_without_the_flag():
    from backend.ticket_scoring import agent_spans, run_ticket_wave_for_agent

    captured_prompts = []

    def _claude(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps({
            "verdict": "pass", "reasoning": "ok",
            "evidence_quote": "Refund approved per policy, internal note only.",
            "evidence_seq": 2,
        })

    dims = [{"id": "ownership", "name": "Ownership", "weight": 15,
             "question": "Was ownership clear across the thread?"}]
    findings = run_ticket_wave_for_agent(
        TURNS_WITH_A_NOTE, dims,
        target_agent_user_id="agent-a", spans=agent_spans(TURNS_WITH_A_NOTE),
        call_claude_fn=_claude,
    )
    assert "Refund approved per policy" in captured_prompts[0]
    assert findings[0]["evidence_verified"] is True


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


def test_resolved_agent_ids_lists_distinct_real_agents_in_order():
    from backend.ticket_scoring import resolved_agent_ids

    assert resolved_agent_ids(TURNS) == ["agent-a", "agent-b"]


def test_resolved_agent_ids_excludes_unresolved_turns():
    from backend.ticket_scoring import resolved_agent_ids

    turns = [
        {"seq": 0, "speaker": "agent", "agent_user_id": None, "text": "hi"},
        {"seq": 1, "speaker": "agent", "agent_user_id": "agent-a", "text": "hello"},
    ]
    assert resolved_agent_ids(turns) == ["agent-a"]


def test_score_ticket_per_agent_gives_each_agent_their_own_independent_finding():
    """TA-21/TA-25's whole point: agent-a and agent-b each get their own
    Claude call for the SAME dimension, not one shared verdict
    post-hoc-attributed by evidence location."""
    from backend.ticket_scoring import score_ticket_per_agent

    dims = [
        {"id": "resolution", "name": "Resolution", "weight": 50,
         "question": "Was the issue resolved?"},
        {"id": "tone", "name": "Tone", "weight": 50,
         "question": "Was the agent professional?"},
    ]

    def _dispatch(prompt: str) -> str:
        if "agent under review" not in prompt:
            raise AssertionError("per-agent prompt must mark who's under review")
        if "Was the issue resolved?" in prompt:
            return json.dumps({
                "verdict": "pass", "reasoning": "resolved it",
                "evidence_quote": "I've restarted the payment worker and the 504 is gone now",
                "evidence_seq": 3,
            })
        return json.dumps({
            "verdict": "pass", "reasoning": "professional tone",
            "evidence_quote": "I can help with that", "evidence_seq": 1,
        })

    results = score_ticket_per_agent(TURNS, dims, call_claude_fn=_dispatch)
    by_agent = {r["agent_user_id"]: r for r in results}
    assert set(by_agent) == {"agent-a", "agent-b"}
    # Each agent gets BOTH dimensions independently scored — four Claude
    # calls total, not two shared ones.
    assert {f["id"] for f in by_agent["agent-a"]["findings"]} == {"resolution", "tone"}
    assert {f["id"] for f in by_agent["agent-b"]["findings"]} == {"resolution", "tone"}


def test_evidence_from_a_different_agents_span_is_downgraded_not_credited():
    """The root bug TA-21 exists to fix, proven directly: if the model
    cites a teammate's turn as evidence, that must never be accepted as
    this agent's own contribution."""
    from backend.ticket_scoring import agent_spans, evaluate_criterion_for_agent

    spans = agent_spans(TURNS)
    # Ask about agent-a, but the stub always cites agent-b's seq 3 turn.
    result = evaluate_criterion_for_agent(
        "Did the agent resolve the issue?",
        TURNS,
        target_agent_user_id="agent-a",
        spans=spans,
        call_claude_fn=_claude_for(
            "I've restarted the payment worker and the 504 is gone now", 3,
        ),
    )
    assert result["verdict"] == "error"
    assert result["evidence_verified"] is False


def test_evidence_within_the_target_agents_own_span_is_accepted():
    from backend.ticket_scoring import agent_spans, evaluate_criterion_for_agent

    spans = agent_spans(TURNS)
    result = evaluate_criterion_for_agent(
        "Was the agent professional?",
        TURNS,
        target_agent_user_id="agent-a",
        spans=spans,
        call_claude_fn=_claude_for("I can help with that", 1),
    )
    assert result["verdict"] == "pass"
    assert result["evidence_verified"] is True


def test_score_renormalises_when_a_dimension_errors():
    from backend.ticket_scoring import score_ticket_for_agent

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

    result = score_ticket_for_agent(
        TURNS, dims, target_agent_user_id="agent-a", call_claude_fn=_dispatch,
    )
    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["bad"]["verdict"] == "error"
    assert result["score"] == 100.0  # only the passing 40-weight dim counts


def test_earned_marks_are_weight_times_points_for_the_verdict():
    """Each finding's `earned` field is what the frontend renders as
    "given/total" next to a criterion — weight for a pass, half-weight
    for a partial, zero for a fail. Not part of _numeric_score's
    renormalization test above; this asserts the per-finding field
    directly."""
    from backend.ticket_scoring import score_ticket_for_agent

    dims = [
        {"id": "passed", "question": "Q-pass", "weight": 20},
        {"id": "partial", "question": "Q-partial", "weight": 30},
        {"id": "failed", "question": "Q-fail", "weight": 50},
    ]

    def _dispatch(prompt: str) -> str:
        if "Q-pass" in prompt:
            verdict = "pass"
        elif "Q-partial" in prompt:
            verdict = "partial"
        else:
            verdict = "fail"
        return json.dumps({
            "verdict": verdict, "reasoning": "r",
            "evidence_quote": "I can help with that", "evidence_seq": 1,
        })

    result = score_ticket_for_agent(
        TURNS, dims, target_agent_user_id="agent-a", call_claude_fn=_dispatch,
    )
    by_id = {f["id"]: f for f in result["findings"]}
    assert by_id["passed"]["earned"] == 20
    assert by_id["partial"]["earned"] == 15
    assert by_id["failed"]["earned"] == 0


def test_earned_is_none_for_not_applicable_and_error_verdicts():
    """A dimension excluded from the weighted score (not_applicable, or
    downgraded to error for foreign evidence) has nothing to show as
    "given/total" — earned is None, not a misleading 0."""
    from backend.ticket_scoring import score_ticket_for_agent

    dims = [{"id": "na", "question": "Q1", "weight": 100}]

    def _not_applicable(prompt: str) -> str:
        return json.dumps({
            "verdict": "not_applicable", "reasoning": "n/a",
            "evidence_quote": "", "evidence_seq": None,
        })

    result = score_ticket_for_agent(
        TURNS, dims, target_agent_user_id="agent-a", call_claude_fn=_not_applicable,
    )
    assert result["findings"][0]["earned"] is None

    def _foreign_evidence(prompt: str) -> str:
        # evidence_seq=3 belongs to agent-b's span, not agent-a's — this
        # gets downgraded to "error" by evidence-ownership enforcement.
        return json.dumps({
            "verdict": "pass", "reasoning": "r",
            "evidence_quote": "restarted the payment worker", "evidence_seq": 3,
        })

    result = score_ticket_for_agent(
        TURNS, dims, target_agent_user_id="agent-a", call_claude_fn=_foreign_evidence,
    )
    assert result["findings"][0]["verdict"] == "error"
    assert result["findings"][0]["earned"] is None


def test_score_ticket_per_agent_does_not_call_run_v8_wave(monkeypatch):
    """Own loop: even if qa_v8.run_v8_wave exists, TA-6 must not touch it."""
    from backend import ticket_scoring

    def _forbidden(*_a, **_k):
        raise AssertionError("run_v8_wave must not be used by the ticket engine")

    monkeypatch.setattr("backend.qa_v8.run_v8_wave", _forbidden, raising=False)
    dims = [{"id": "x", "question": "Did the agent help?", "weight": 10}]
    results = ticket_scoring.score_ticket_per_agent(
        TURNS, dims,
        # seq 1 is agent-a's own turn — a real match only for agent-a's
        # independent call; agent-b's call correctly downgrades it as
        # foreign evidence, which is exactly what this fixture is for
        # elsewhere. This test only cares that run_v8_wave was never hit.
        call_claude_fn=_claude_for("I can help with that", 1),
    )
    by_agent = {r["agent_user_id"]: r for r in results}
    assert by_agent["agent-a"]["findings"][0]["verdict"] == "pass"
