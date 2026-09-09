"""IN-12: audit_summary/top_strength/top_gap, generated exclusively from
CallLoop's own per-dimension scoring output — never a neutral transcript
recap (Intercom's conversation_summary, or PyAI's Recap). Pure functions,
no DB, no Claude call — fully deterministic."""

from __future__ import annotations

from backend import ticket_audit_summary as m

FINDINGS = [
    {"id": "tone", "name": "Tone", "weight": 15, "verdict": "pass",
     "reasoning": "The agent stayed calm and empathetic throughout.",
     "evidence_text": "I understand your frustration.", "evidence_seq": 1},
    {"id": "diagnostic_reasoning", "name": "Diagnostic Reasoning", "weight": 20, "verdict": "pass",
     "reasoning": "Correctly identified the root cause from the logs.",
     "evidence_text": "It's the payment worker.", "evidence_seq": 3},
    {"id": "investigation_rigor", "name": "Investigation Rigor", "weight": 20, "verdict": "fail",
     "reasoning": "Never checked logs before guessing at a fix.",
     "evidence_text": "Try clearing your cache.", "evidence_seq": 2},
    {"id": "escalation_quality", "name": "Escalation Quality", "weight": 10, "verdict": "partial",
     "reasoning": "Escalated, but with limited context.",
     "evidence_text": "Passing to tier 2.", "evidence_seq": 4},
]


def test_top_strength_picks_the_highest_weighted_pass():
    result = m.top_strength(FINDINGS)
    assert result["id"] == "diagnostic_reasoning"  # weight 20 > tone's 15
    assert result["weight"] == 20
    assert result["reasoning"] == "Correctly identified the root cause from the logs."


def test_top_gap_picks_the_highest_weighted_fail():
    result = m.top_gap(FINDINGS)
    assert result["id"] == "investigation_rigor"
    assert result["weight"] == 20


def test_partial_verdicts_are_neither_strength_nor_gap():
    only_partial = [FINDINGS[3]]
    assert m.top_strength(only_partial) is None
    assert m.top_gap(only_partial) is None


def test_top_strength_none_when_nothing_passed():
    all_fail = [{**f, "verdict": "fail"} for f in FINDINGS]
    assert m.top_strength(all_fail) is None
    assert m.top_gap(all_fail) is not None


def test_top_gap_none_when_nothing_failed():
    all_pass = [{**f, "verdict": "pass"} for f in FINDINGS]
    assert m.top_gap(all_pass) is None
    assert m.top_strength(all_pass) is not None


def test_empty_findings_list():
    assert m.top_strength([]) is None
    assert m.top_gap([]) is None
    assert m.generate_audit_summary([]) == "Not enough scored dimensions to summarize."


def test_tie_break_is_the_first_encountered_in_list_order():
    tied = [
        {"id": "a", "name": "A", "weight": 20, "verdict": "pass", "reasoning": "first"},
        {"id": "b", "name": "B", "weight": 20, "verdict": "pass", "reasoning": "second"},
    ]
    assert m.top_strength(tied)["id"] == "a"


def test_generate_audit_summary_includes_both_strength_and_gap():
    summary = m.generate_audit_summary(FINDINGS)
    assert "Strongest on Diagnostic Reasoning" in summary
    assert "root cause" in summary
    assert "Needs improvement on Investigation Rigor" in summary
    assert "checked logs" in summary


def test_generate_audit_summary_degrades_gracefully_with_no_gap():
    all_pass = [{**f, "verdict": "pass"} for f in FINDINGS]
    summary = m.generate_audit_summary(all_pass)
    assert "Strongest on" in summary
    assert "Needs improvement" not in summary


def test_generate_audit_summary_degrades_gracefully_with_no_strength():
    all_fail = [{**f, "verdict": "fail"} for f in FINDINGS]
    summary = m.generate_audit_summary(all_fail)
    assert "Needs improvement on" in summary
    assert "Strongest on" not in summary


def test_generate_audit_summary_never_double_periods():
    findings = [
        {"id": "tone", "name": "Tone", "weight": 15, "verdict": "pass",
         "reasoning": "Ends with a period already."},
    ]
    summary = m.generate_audit_summary(findings)
    assert ".." not in summary


def test_never_reads_a_conversation_summary_or_transcript_field():
    """IN-12's whole point: this module must never accept or read
    anything resembling a transcript/neutral-recap field. The module AND
    every function's docstring discuss conversation_summary/recap/
    transcript at length as prose explaining why they're NOT used, so
    this walks the AST and excludes every docstring node specifically
    (not just the module's), then checks every remaining identifier and
    string literal — a real semantic check, not line-prefix guessing."""
    import ast

    from backend.paths import ROOT

    src = (ROOT / "backend" / "ticket_audit_summary.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = {"conversation_summary", "recap", "transcript"}

    docstring_ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr):
                val = body[0].value
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    docstring_ids.add(id(val))

    for node in ast.walk(tree):
        if id(node) in docstring_ids:
            continue
        if isinstance(node, ast.Name) and node.id.lower() in banned:
            raise AssertionError(f"unexpected identifier reference: {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.strip().lower() in banned:
            raise AssertionError(f"unexpected string literal: {node.value!r}")
