"""Ticket Audit Engine: the real "Ticket QA" rubric (TA-20/TA-23/TA-24,
Sellable Release PRD §3, 2026-09-12 — replaces TA-7's scaffold content
outright).

PRD §10: a Ticket QA rubric is a normal entry in the existing `rubrics`
library — no schema change, since that table was already designed to
hold more than one rubric per org (0022_tickets.py's own docstring
anticipated exactly this: "a Ticket QA rubric (TA-13) is another
org-scoped versioned row in the existing table"). ensure_ticket_rubric()
below creates that row the first time an org scores a ticket, so scoring
is genuinely backed by a real rubrics-table entry, not just this file's
in-memory constant.

Content, grounded in what the Intercom ingestion pipeline actually
proves is present in real ticket data — not generic, not guessed: real
sample payloads examined during that epic showed decisive,
customer-impacting decisions delivered through internal notes (a
refund-policy determination), multi-agent handoffs on a single case, and
real per-message timestamps.

  - Problem Diagnosis (22%): did the agent correctly identify the
    customer's actual issue before acting — evidenced against the
    agent's own turns, not whether the ticket was eventually closed.
  - Resolution Correctness (27%): was the fix/decision actually correct
    against policy and the issue, not just "a resolution was offered."
    Internal notes are evidence here, not just customer-facing replies —
    the exact refund-policy-in-a-note case this rubric is grounded in.
  - Communication Clarity (17%) and Tone & Empathy (17%): both
    customer_facing_only=True, same reasoning IN-9 already established
    for the single "Tone" dimension in the old scaffold — these two are
    unambiguously about customer-visible communication, unlike the other
    three, which are deliberately left able to see internal notes.
  - Ownership & Handoff Quality (17%): the async, multi-touch pattern
    tickets structurally have and calls don't — a ticket can bounce
    between agents and span days.
  - Response Timeliness: unchanged, deterministic, informational only
    (see evaluate_response_timeliness() below) — not folded into the
    weighted score, shown per agent span once the Per-Agent Ticket Audit
    epic (TA-21) ships.

Weights were originally specified summing to 90% (TA-23) — rescaled
proportionally (×1.111, rounded, adjusted by one point) to land on 100%
without changing any dimension's relative emphasis.

Shares zero code with rules_v8.py (the call engine's rubric dispatch) —
LLM-judged questions have no deterministic `method` dispatch.
ticket_scoring.evaluate_criterion() only ever reads id/name/weight/
question off a dimension dict.
"""

from __future__ import annotations

import copy
import uuid
from datetime import datetime, timedelta

from psycopg.types.json import Json

from . import db
from .org_ids import org_scope

TICKET_QA_RUBRIC_NAME = "Ticket QA"
_RUBRIC_KIND = "ticket"

TICKET_QA_DIMENSIONS: list[dict] = [
    {
        "id": "problem_diagnosis",
        "name": "Problem Diagnosis",
        "weight": 22,
        "question": (
            "Did the agent correctly identify the customer's actual "
            "underlying issue, using the technical details available in "
            "the thread (including any screenshots described in it), "
            "before acting on it?"
        ),
    },
    {
        "id": "resolution_correctness",
        "name": "Resolution Correctness",
        "weight": 27,
        "question": (
            "Was the fix or decision the agent actually made correct "
            "against policy and the diagnosed issue — not just whether a "
            "resolution was offered? Internal notes count as evidence "
            "here: a decision recorded only in a note (e.g. a refund "
            "determination) is still the resolution being judged."
        ),
    },
    {
        "id": "communication_clarity",
        "name": "Communication Clarity",
        "weight": 17,
        "question": (
            "Were the agent's customer-facing replies clear, free of "
            "unexplained jargon, and easy for the customer to act on?"
        ),
        # Unambiguously about customer-visible language — judging it against
        # an internal note the customer never saw would score wording they
        # never read.
        "customer_facing_only": True,
    },
    {
        "id": "tone_and_empathy",
        "name": "Tone & Empathy",
        "weight": 17,
        "question": (
            "Did the agent maintain a professional, empathetic tone "
            "toward the customer throughout the ticket, even if the "
            "customer was frustrated?"
        ),
        # Same reasoning as Communication Clarity — tone toward a customer
        # can't be judged from a note they never saw.
        "customer_facing_only": True,
    },
    {
        "id": "ownership_and_handoff_quality",
        "name": "Ownership & Handoff Quality",
        "weight": 17,
        "question": (
            "Across the whole thread — even as it may have passed between "
            "different agents or spanned multiple days — was it always "
            "clear who owned the next step? When the ticket changed hands, "
            "did the new agent pick up with full context rather than "
            "leaving the ticket to go quiet or making the customer "
            "re-explain what already happened?"
        ),
        # Deliberately NOT customer_facing_only — a clean handoff is judged
        # from internal notes/context passed between agents as much as from
        # what the customer saw, consistent with IN-9's reasoning for
        # Resolution Correctness above.
    },
]

_TIMELINESS_PASS_MAX = timedelta(hours=4)
_TIMELINESS_PARTIAL_MAX = timedelta(hours=24)


def get_default_ticket_rubric() -> list[dict]:
    """A fresh copy of the five LLM-judged dimensions — callers can't
    accidentally mutate the module-level constant."""
    return copy.deepcopy(TICKET_QA_DIMENSIONS)


def _as_datetime(value) -> datetime | None:
    """Accepts a real datetime (from ticket_pdf_parser directly) or an
    ISO string (ticket_ingest.get_ticket()'s JSON-safe _iso() conversion)
    — this function is reachable from both a fresh parse and a value
    that's already round-tripped through an API response."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def evaluate_response_timeliness(
    turns: list[dict], *, target_agent_user_id: str | None = None,
) -> dict:
    """TA-13/TA-27's dimension: the longest real wait between a customer
    message and the next AGENT reply (bot replies don't count as the
    agent responding). Deterministic — a computed number, not a judgment
    call — so it never calls Claude, unlike the five LLM-judged
    TICKET_QA_DIMENSIONS.

    TA-27: target_agent_user_id scopes this to one specific agent's own
    responsiveness — only a gap ending in *that* agent's reply counts,
    so a fast-replying agent's score isn't dragged down by a teammate's
    slow one on the same ticket, and vice versa. Omitting it keeps the
    old ticket-wide "worst gap by anyone" measurement.

    Needs turn["sent_at"] (ticket_pdf_parser.py's real per-message
    timestamps, TA-13). A ticket with no timestamps at all (ingested
    before TA-13, or a hand-built test fixture) can't be measured —
    verdict "error" rather than a guess. A ticket (or agent) where no
    customer message ever waited on a reply gets "not_applicable".

    Not folded into score_ticket_for_agent()'s weighted score — the
    aggregate-scoring design is exactly the "final design" work PRD §10
    defers to later. Returned as its own finding for the caller to
    display, not to sum in.
    """
    ordered = [
        {**t, "sent_at": _as_datetime(t.get("sent_at"))}
        for t in sorted(turns, key=lambda t: t["seq"])
    ]
    if not any(t["sent_at"] for t in ordered):
        return {
            "id": "response_timeliness",
            "name": "Response Timeliness",
            "verdict": "error",
            "reasoning": "No message timestamps on this ticket to measure from.",
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
            "deterministic": True,
        }

    worst_gap: timedelta | None = None
    worst_seq: int | None = None
    waiting_since: datetime | None = None
    for t in ordered:
        if t["speaker"] == "customer":
            if waiting_since is None and t["sent_at"]:
                waiting_since = t["sent_at"]
            continue
        if t["speaker"] != "agent":
            continue  # a bot reply doesn't count as the agent responding
        is_target = (
            target_agent_user_id is None
            or str(t.get("agent_user_id") or "") == str(target_agent_user_id)
        )
        if waiting_since is not None and t["sent_at"] and is_target:
            gap = t["sent_at"] - waiting_since
            if worst_gap is None or gap > worst_gap:
                worst_gap = gap
                worst_seq = t["seq"]
        if waiting_since is not None and t["sent_at"]:
            # Any agent's reply — target or not — ends this customer's wait;
            # only whether it COUNTS toward the target's worst gap differs.
            waiting_since = None

    if worst_gap is None:
        return {
            "id": "response_timeliness",
            "name": "Response Timeliness",
            "verdict": "not_applicable",
            "reasoning": (
                "No customer message was ever waiting on a reply from this agent."
                if target_agent_user_id is not None
                else "No customer message was ever waiting on an agent reply."
            ),
            "evidence_text": None,
            "evidence_seq": None,
            "evidence_verified": False,
            "deterministic": True,
        }

    if worst_gap <= _TIMELINESS_PASS_MAX:
        verdict = "pass"
    elif worst_gap <= _TIMELINESS_PARTIAL_MAX:
        verdict = "partial"
    else:
        verdict = "fail"

    hours = worst_gap.total_seconds() / 3600
    return {
        "id": "response_timeliness",
        "name": "Response Timeliness",
        "verdict": verdict,
        "reasoning": f"Longest wait for a reply was {hours:.1f} hours.",
        "evidence_text": None,
        "evidence_seq": worst_seq,
        "evidence_verified": True,
        "deterministic": True,
    }


def _default_ticket_definition() -> dict:
    return {"kind": _RUBRIC_KIND, "dimensions": get_default_ticket_rubric()}


def fetch_active_ticket_rubric(org_id: str) -> dict | None:
    """The org's active Ticket QA rubric, or None if never seeded.

    Filters explicitly on definition->>'kind' = 'ticket' — the rubrics
    table's is_active column is shared with call rubrics (PRD §10: no
    schema change), so this must never pick up a call rubric row. See
    audit_store.fetch_active_rubric()'s matching exclusion on the other
    side, which keeps the two engines from ever reading each other's
    active rubric.
    """
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT id, name, version, definition, updated_at
                FROM rubrics
                WHERE org_id = %s AND is_active
                  AND definition->>'kind' = %s
                LIMIT 1
                """,
                (org_id, _RUBRIC_KIND),
            ).fetchone()
    if not row:
        return None
    definition = row["definition"] if isinstance(row["definition"], dict) else {}
    return {
        "id": str(row["id"]),
        "name": row["name"],
        "version": row["version"],
        "updated_at": row.get("updated_at"),
        "dimensions": definition.get("dimensions") or [],
    }


def ensure_ticket_rubric(org_id: str) -> dict:
    """The org's active Ticket QA rubric — seeds a default v1 row the
    first time an org scores a ticket, so scoring is backed by a real
    rubrics-table entry (PRD §10), not just this file's constant.

    Cross-kind write safety (call rubric saves/activates never touching
    this row, and vice versa) is now a real invariant, not a caveat: see
    audit_store.py's kind= parameter on save_named_rubric/
    activate_rubric_by_name/etc. (generalized 2026-09-16, TA-24
    follow-on, ticket_rubric_builder.py) — self-healing re-seeding here
    is no longer the load-bearing recovery path it used to be.
    """
    existing = fetch_active_ticket_rubric(org_id)
    if existing is not None:
        return existing
    rubric_id = str(uuid.uuid4())
    definition = _default_ticket_definition()
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
                VALUES (%s, %s, %s, %s, %s, true)
                ON CONFLICT (org_id, name) WHERE is_active DO NOTHING
                """,
                (rubric_id, org_id, TICKET_QA_RUBRIC_NAME, 1, Json(definition)),
            )
    return fetch_active_ticket_rubric(org_id) or {
        "id": rubric_id, "name": TICKET_QA_RUBRIC_NAME, "version": 1,
        "updated_at": None, "dimensions": definition["dimensions"],
    }
