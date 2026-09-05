"""Ticket Audit Engine (TA-12): manager full-thread view vs. agent
own-contribution view.

No new mechanism (PRD §7) — reuses the same narrow-then-broad shape as
require_owner/require_platform_admin elsewhere in this codebase: a broad
check (is the caller the org's owner, i.e. "manager") decides whether a
narrower per-actor filter applies at all. org_members.role is only
"owner"/"member" today; "owner" stands in for "manager" until a
team-admin tier ships — the same substitution auth.is_org_owner() already
makes for require_owner().

A manager sees the whole thread and every agent's findings. Any other
member sees only turns that fall inside a span attributed to their own
agent_user_id, and only findings attributed to them — never another
agent's individual scores, even on a thread they share.

Reuses ticket_scoring.agent_spans() rather than reimplementing span
logic — the same reasoning ticket_scoring.py itself gives for reusing
evidence verification instead of a bespoke attribution mechanism.
"""

from __future__ import annotations

from . import ticket_scoring


def _own_spans(spans: list[dict], viewer_user_id: str) -> list[dict]:
    return [
        s for s in spans
        if s.get("agent_user_id") and str(s["agent_user_id"]) == str(viewer_user_id)
    ]


def filter_turns_for_viewer(
    turns: list[dict], *, viewer_user_id: str, is_manager: bool,
) -> list[dict]:
    """The full thread for a manager. Otherwise, only the turns inside a
    span attributed to the viewer's own agent_user_id — turns before any
    agent has spoken, or inside a different agent's span, are not part
    of "their own contribution" and are dropped."""
    if is_manager:
        return list(turns)
    spans = ticket_scoring.agent_spans(turns)
    own = _own_spans(spans, viewer_user_id)
    if not own:
        return []
    return [
        t for t in turns
        if any(span["start_seq"] <= t["seq"] <= span["end_seq"] for span in own)
    ]


def filter_findings_for_viewer(
    findings: list[dict], *, viewer_user_id: str, is_manager: bool,
) -> list[dict]:
    """Every finding for a manager. Otherwise only findings attributed to
    the viewer — never another agent's individual verdict, even on a
    thread they share."""
    if is_manager:
        return list(findings)
    return [
        f for f in findings
        if f.get("attributed_to") and str(f["attributed_to"]) == str(viewer_user_id)
    ]


def filter_spans_for_viewer(
    spans: list[dict], *, viewer_user_id: str, is_manager: bool,
) -> list[dict]:
    """Every span for a manager. Otherwise only the viewer's own span(s)."""
    if is_manager:
        return list(spans)
    return _own_spans(spans, viewer_user_id)
