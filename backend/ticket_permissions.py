"""Ticket Audit Engine (TA-12, rebuilt TA-21/TA-29/TA-30): manager
full-breakdown view vs. agent own-scorecard view.

TA-30 fixed a real bug found live: the old filter_turns_for_viewer
stripped every turn outside a non-manager's own span, so "My
Contribution" (and a shared ticket's detail page) hid the rest of the
thread entirely — an agent reviewing their own work couldn't see what
the customer originally asked, or what a teammate did before or after
them. The full thread is now always returned to every viewer,
regardless of role; only which agent's *scorecard(s)* a viewer sees is
still access-controlled. own_span_seqs is what the frontend uses to
highlight a viewer's own turns in that full thread instead of isolating
them.

A manager (auth.is_owner_or_manager(), AC-56/AC-60) sees every agent's
independent scorecard (TA-28's one-row-per-agent model). Any other
member sees only their own scorecard — never a teammate's individual
score, even on a thread they share.
"""

from __future__ import annotations

from . import ticket_scoring


def own_span_seqs(turns: list[dict], viewer_user_id: str) -> list[int]:
    """Every seq inside a span attributed to the viewer's own
    agent_user_id — the highlight set for the full thread (TA-30). Empty
    if the viewer never appears as an agent on this ticket."""
    spans = ticket_scoring.agent_spans(turns)
    own = [
        s for s in spans
        if s.get("agent_user_id") and str(s["agent_user_id"]) == str(viewer_user_id)
    ]
    seqs: list[int] = []
    for span in own:
        seqs.extend(
            t["seq"] for t in turns if span["start_seq"] <= t["seq"] <= span["end_seq"]
        )
    return sorted(set(seqs))


def filter_audits_for_viewer(
    audits: list[dict], *, viewer_user_id: str, is_manager: bool,
) -> list[dict]:
    """Every agent's stored scorecard for a manager. Otherwise only the
    viewer's own — never a teammate's individual score, even on a
    thread they share."""
    if is_manager:
        return list(audits)
    return [
        a for a in audits
        if a.get("agent_user_id") and str(a["agent_user_id"]) == str(viewer_user_id)
    ]
