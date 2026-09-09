"""IN-12: audit_summary, Top Strength, and Top Gap - generated exclusively
from CallLoop's own per-dimension scoring output, never from a neutral
transcript recap. Intercom's `conversation_summary` and PyAI's Recap
(backend/recap.py) are the same category of tool from two different
providers - a factual "what happened," not an evaluative "how did the
agent do." Neither is a substitute for this module's job; Intercom's
conversation_summary may only ever be passed in as background context to
a future richer version (see below), never as a stand-in for it.

Computed fresh on every read from a ticket's `findings`, never persisted
- same reasoning ticket_score_api._with_timeliness() already documents
for Response Timeliness: purely deterministic and cheap to recompute, so
storing a stale copy alongside `findings` (already the source of truth)
would only risk drift for no benefit.

Callers must pass already-viewer-filtered findings (TA-12), not the raw
scoring output - an agent viewing their own scorecard must see a Top
Strength/Top Gap/audit_summary computed from only their own attributed
findings, never another agent's, matching the same "my score reflects
only what I actually contributed" principle IN-10 already states for
identity mapping. This module has no awareness of viewers or permissions
itself; it only ever sees whatever the caller already decided is visible.

Top Strength / Top Gap stay fully deterministic - no Claude call -
picked by highest weight among pass/fail verdicts respectively (ties
broken by original rubric-dimension order, since Python's max() returns
the first-encountered maximum on a tie). A "partial" verdict is neither:
not a clean win to hold up as a strength, nor a clean miss to flag as a
gap.

audit_summary is a short, stitched sentence built directly from Top
Strength/Top Gap's own reasoning text - no LLM call for v1. The story
also allows "one small, dedicated Claude call over that scoring output"
as a valid alternative; not built here, since the deterministic version
already satisfies the acceptance criterion (an evaluative summary
grounded in CallLoop's own scoring, not a neutral recap) at zero added
cost or latency. A real LLM-based version - taking Intercom's
conversation_summary as background context, exactly as the story
permits - is a reasonable v2 if a richer prose summary ever turns out to
be worth the added cost; nothing here forecloses it.
"""

from __future__ import annotations


def _clean(text: str | None) -> str:
    return (text or "").strip().rstrip(". ")


def _best_of(findings: list[dict], verdict: str) -> dict | None:
    candidates = [f for f in findings if f.get("verdict") == verdict]
    if not candidates:
        return None
    best = max(candidates, key=lambda f: f.get("weight") or 0)
    return {
        "id": best.get("id"),
        "name": best.get("name"),
        "weight": best.get("weight"),
        "reasoning": best.get("reasoning"),
        "evidence_text": best.get("evidence_text"),
        "evidence_seq": best.get("evidence_seq"),
    }


def top_strength(findings: list[dict]) -> dict | None:
    """The highest-weighted dimension with verdict "pass", or None if
    there is no passing dimension to hold up as a strength."""
    return _best_of(findings, "pass")


def top_gap(findings: list[dict]) -> dict | None:
    """The highest-weighted dimension with verdict "fail", or None if
    there is no failing dimension to flag."""
    return _best_of(findings, "fail")


def generate_audit_summary(findings: list[dict]) -> str:
    """A short, evaluative sentence stitched from Top Strength/Top Gap -
    grounded entirely in CallLoop's own scoring, never a transcript
    recap. Degrades gracefully when one or both sides are missing (a
    ticket with no clear strength, or no clear gap, still gets a real
    sentence back, not an empty string)."""
    strength = top_strength(findings)
    gap = top_gap(findings)
    if strength is None and gap is None:
        return "Not enough scored dimensions to summarize."
    parts = []
    if strength:
        parts.append(f"Strongest on {strength['name']}: {_clean(strength['reasoning'])}")
    if gap:
        parts.append(f"Needs improvement on {gap['name']}: {_clean(gap['reasoning'])}")
    return ". ".join(parts) + "."
