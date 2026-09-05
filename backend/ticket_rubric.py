"""Ticket Audit Engine (TA-7): a ticket-native placeholder rubric.

*** SCAFFOLDING — NOT PRODUCTION-READY. Do not treat this as the real
ticket rubric. *** PRD §11 names exactly this risk: a scaffold rubric
"could get treated as production-ready by mistake" if it isn't labeled
clearly everywhere it shows up. Every dimension below carries
`"scaffold": True` for that reason — any caller (API response, admin UI,
a future report) can and should surface that flag rather than assume
these six criteria/weights are final. The real rubric is a separate,
future piece of work (TA-13: a proper org-scoped, versioned row in the
existing `rubrics` table, same as call rubrics). Per the PRD's own
guidance, this file deliberately does not try to anticipate that design —
it exists only to give TA-6 (ticket_scoring.py) something real to run
end-to-end against before the ingestion pipeline itself is validated
(TA-14). Don't extend or "improve" this rubric in place of doing TA-13
properly.

Shares zero code with rules_v8.py (the call engine's rubric dispatch) —
these are plain LLM-judged questions, no deterministic method, no
`method` dispatch at all. `ticket_scoring.evaluate_criterion()` only
ever reads `id` / `name` / `weight` / `question` off a dimension dict,
so that's all a scaffold needs to provide.

The six criteria are the ones the PRD names for the eventual v2 per-span
design (§6): Tone, Ownership, Diagnostic Reasoning, and Investigation
Rigor are agent-specific; Resolution Effectiveness and Escalation
Quality are outcome criteria attributed to whichever span the deciding
action falls in. v1 (TA-8) scores all six against the whole thread once
and attributes the audit to a single primary owner — see
ticket_scoring.score_ticket()'s docstring — rather than per span.
"""

from __future__ import annotations

import copy

SCAFFOLD_TICKET_RUBRIC: list[dict] = [
    {
        "id": "tone",
        "name": "Tone",
        "weight": 15,
        "question": (
            "Did the agent maintain a professional, empathetic tone "
            "throughout the ticket, even if the customer was frustrated?"
        ),
        "scaffold": True,
    },
    {
        "id": "ownership",
        "name": "Ownership",
        "weight": 15,
        "question": (
            "Did the agent take clear ownership of the customer's issue "
            "rather than deflecting or leaving it ambiguous who was "
            "responsible for the next step?"
        ),
        "scaffold": True,
    },
    {
        "id": "diagnostic_reasoning",
        "name": "Diagnostic Reasoning",
        "weight": 20,
        "question": (
            "Did the agent correctly diagnose the underlying cause of the "
            "customer's problem, using the technical details available in "
            "the thread (including any screenshots described in it)?"
        ),
        "scaffold": True,
    },
    {
        "id": "investigation_rigor",
        "name": "Investigation Rigor",
        "weight": 20,
        "question": (
            "Did the agent investigate thoroughly before responding — "
            "checking logs, reproducing the issue, or asking clarifying "
            "questions — rather than guessing at a fix?"
        ),
        "scaffold": True,
    },
    {
        "id": "resolution_effectiveness",
        "name": "Resolution Effectiveness",
        "weight": 20,
        "question": (
            "Was the customer's issue actually resolved by the end of "
            "the thread, not just acknowledged?"
        ),
        "scaffold": True,
    },
    {
        "id": "escalation_quality",
        "name": "Escalation Quality",
        "weight": 10,
        "question": (
            "If the issue required escalation or handoff, was it "
            "escalated promptly and with enough context for the next "
            "agent to continue without re-asking the customer?"
        ),
        "scaffold": True,
    },
]


def get_scaffold_rubric() -> list[dict]:
    """A fresh copy of the scaffold rubric — callers can't accidentally
    mutate the module-level constant. Not a stand-in for a real
    rubric-loading API; there is no org-scoping or versioning here."""
    return copy.deepcopy(SCAFFOLD_TICKET_RUBRIC)
