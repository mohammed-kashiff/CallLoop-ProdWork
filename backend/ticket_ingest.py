"""Ticket Audit Engine (TA-4 write path): persist a parsed ticket PDF's
turns into tickets / ticket_messages (TA-3, alembic/versions/0022_tickets.py).

Independent of the call-scoring engine and of transcribe.py — no audio,
no PyAI Hear. Writes go through org_scope()/db.connection(), the same
RLS-safe pattern as every other tenant write in this codebase — the
session role is never granted a way around row-level security.

ticket_messages.agent_user_id is nullable and only ever set when the
caller already has a resolved org_members.user_id for that turn's
speaker — ticket_pdf_parser.parse_ticket_pdf() always returns None for
it today (see that module's docstring for why), so every write here is
NULL until an agent-identity-resolution step exists upstream.

Note: the parser also returns speaker_name (the raw display name off the
PDF, e.g. "Kashif") for turns where agent_user_id can't be resolved.
ticket_messages (TA-3) has no column to hold that, so it is intentionally
dropped here rather than persisted — flagged for whoever picks up TA-8
(multi-agent attribution), not something to fix by altering TA-3's schema
unilaterally.
"""

from __future__ import annotations

from . import db
from . import ticket_pdf_parser
from .org_ids import org_scope


def create_ticket(org_id: str, *, source: str = "pdf_upload") -> str:
    """One row in `tickets`. status defaults to 'uploaded'. Returns the new id."""
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO tickets (org_id, source)
                VALUES (%s, %s)
                RETURNING id
                """,
                (org_id, source),
            ).fetchone()
    return str(row["id"])


def set_ticket_status(ticket_id: str, org_id: str, status: str) -> None:
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                "UPDATE tickets SET status = %s WHERE id = %s AND org_id = %s",
                (status, ticket_id, org_id),
            )


def insert_ticket_messages(ticket_id: str, org_id: str, turns: list[dict]) -> None:
    """Bulk-insert parsed turns (ticket_pdf_parser's output shape) into
    ticket_messages, in seq order. No-op for an empty list."""
    if not turns:
        return
    with org_scope(org_id):
        with db.connection() as conn:
            for t in turns:
                conn.execute(
                    """
                    INSERT INTO ticket_messages
                        (ticket_id, org_id, seq, agent_user_id, speaker, text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ticket_id, org_id, t["seq"], t["agent_user_id"],
                        t["speaker"], t["text"],
                    ),
                )


def ingest_ticket_pdf(org_id: str, pdf_bytes: bytes, *, source: str = "pdf_upload") -> str:
    """Full TA-4 write path: create the ticket row, parse the PDF against
    the confirmed JustCall template (TA-2), write the turns, and move the
    ticket to 'ready' for scoring (TA-6) — or 'failed' if the PDF doesn't
    match the known template. Returns the ticket id either way.
    """
    ticket_id = create_ticket(org_id, source=source)
    set_ticket_status(ticket_id, org_id, "processing")
    try:
        turns = ticket_pdf_parser.parse_ticket_pdf(pdf_bytes)
    except ValueError:
        set_ticket_status(ticket_id, org_id, "failed")
        raise
    insert_ticket_messages(ticket_id, org_id, turns)
    set_ticket_status(ticket_id, org_id, "ready")
    return ticket_id
