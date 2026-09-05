"""Ticket Audit Engine (TA-7 scaffold content, TA-13 real storage): the
"Ticket QA" rubric.

PRD §10: a Ticket QA rubric is a normal entry in the existing `rubrics`
library — no schema change, since that table was already designed to
hold more than one rubric per org (0022_tickets.py's own docstring
anticipated exactly this: "a Ticket QA rubric (TA-13) is another
org-scoped versioned row in the existing table"). ensure_ticket_rubric()
below creates that row the first time an org scores a ticket, so scoring
is genuinely backed by a real rubrics-table entry, not just this file's
in-memory constant.

*** Still not the final rubric design. *** None of this needs to be
finalized for v1 (PRD §4/§10/TA-7's own scaffolding note) — this ships
enough real content to validate the pipeline (TA-14). The full rubric
design (final weights, dimension set, per-span v2 scoring) is a separate,
later effort once the pipeline itself is proven (PRD §12 step 4). Every
dimension still carries "scaffold": True for that reason.

Content, per PRD §10:
  - Diagnostic Reasoning, Investigation Rigor, Escalation Quality: carried
    over largely unchanged from TA-7 — these judge technical reasoning
    against an issue, not against a call specifically, so they transfer.
  - Ownership: genuinely reworked for the async, multi-touch pattern — a
    ticket can bounce between agents and span days, which a single
    synchronous call never has to account for.
  - Response Timeliness: new. Tickets enable this and calls structurally
    can't — see evaluate_response_timeliness() below, which is
    deterministic (real elapsed time between messages), not an LLM
    judgment call, and is NOT folded into score_ticket()'s weighted
    score for v1 — shown as its own informational finding instead.

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
            "Across the whole thread — even as it may have passed between "
            "different agents or spanned multiple days — was it always "
            "clear who owned the next step? When the ticket changed hands, "
            "did the new agent pick up with full context rather than "
            "leaving the ticket to go quiet or making the customer "
            "re-explain what already happened?"
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

_TIMELINESS_PASS_MAX = timedelta(hours=4)
_TIMELINESS_PARTIAL_MAX = timedelta(hours=24)


def get_scaffold_rubric() -> list[dict]:
    """A fresh copy of the six LLM-judged dimensions — callers can't
    accidentally mutate the module-level constant."""
    return copy.deepcopy(SCAFFOLD_TICKET_RUBRIC)


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


def evaluate_response_timeliness(turns: list[dict]) -> dict:
    """TA-13's new dimension: the longest real wait between a customer
    message and the next AGENT reply (bot replies don't count as the
    agent responding). Deterministic — a computed number, not a
    judgment call — so it never calls Claude and is scaffold=False;
    "scaffold" here describes the LLM-judged six, not this one.

    Needs turn["sent_at"] (ticket_pdf_parser.py's real per-message
    timestamps, TA-13). A ticket with no timestamps at all (ingested
    before TA-13, or a hand-built test fixture) can't be measured —
    verdict "error" rather than a guess. A ticket where no customer
    message ever waited on an agent gets "not_applicable".

    Not folded into score_ticket()'s weighted score for v1 — the
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
        if waiting_since is not None and t["sent_at"]:
            gap = t["sent_at"] - waiting_since
            if worst_gap is None or gap > worst_gap:
                worst_gap = gap
                worst_seq = t["seq"]
        waiting_since = None

    if worst_gap is None:
        return {
            "id": "response_timeliness",
            "name": "Response Timeliness",
            "verdict": "not_applicable",
            "reasoning": "No customer message was ever waiting on an agent reply.",
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
        "reasoning": f"Longest wait for an agent reply was {hours:.1f} hours.",
        "evidence_text": None,
        "evidence_seq": worst_seq,
        "evidence_verified": True,
        "deterministic": True,
    }


def _default_ticket_definition() -> dict:
    return {"kind": _RUBRIC_KIND, "dimensions": get_scaffold_rubric()}


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
                SELECT id, name, version, definition
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
        "dimensions": definition.get("dimensions") or [],
    }


def ensure_ticket_rubric(org_id: str) -> dict:
    """The org's active Ticket QA rubric — seeds a default v1 row the
    first time an org scores a ticket, so scoring is backed by a real
    rubrics-table entry (PRD §10), not just this file's constant.

    Known limitation: rubric_builder.py's save/activate functions for
    CALL rubrics deactivate every active row for the org with no kind
    filter, so activating a call rubric could silently deactivate this
    row. Not audited/fixed here — v1 accepts self-healing re-seeding
    (the next call here just creates a fresh one) as the recovery path
    rather than auditing every call-rubric write path, which is a
    separate, later effort.
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
        "dimensions": definition["dimensions"],
    }
