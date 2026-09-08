"""
CallProof - Intercom conversation ingest (IN-5/IN-7, conversation half only).

Normalizes a fetched Intercom Conversation object into the same canonical
turn shape ticket_pdf_parser.parse_turns() produces — {seq, speaker,
speaker_name, agent_user_id, text, sent_at} — so ticket_ingest.py's
existing DB-writing functions and the scoring engine need zero changes to
handle an Intercom-sourced ticket alongside a PDF-sourced one.

Deliberately conversation-only. Ticket-topic webhooks (ticket.resolved,
ticket.closed) are received and acknowledged by the webhook route but not
normalized here yet — Intercom's ticket object schema was never confirmed
against a real payload (same open risk the original PRD flagged in its own
Assumptions & Risks section), so guessing at its shape risks silently
wrong data more than it's worth. That's IN-8's job, against a real
ticket-first sample.

Two things confirmed here that the PRD's own field-mapping table (built
from real payloads, but apparently not exhaustively) didn't cover, checked
against Intercom's published API reference before writing this:
  - A conversation's opening message lives in `source`, a separate object
    from `conversation_parts` — conversation_parts is only the replies/
    notes that came AFTER the first message. Skipping `source` would
    silently drop every conversation's opening line.
  - The parts array is nested: `conversation_parts.conversation_parts`,
    not a flat top-level `conversation_parts` list.

agent_user_id is always None here, same honest gap ticket_pdf_parser
already documents for PDF-sourced tickets — IN-10 (email-keyed agent
identity resolution) needs its own schema decision before this can resolve
to a real org_members user_id.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from . import applog
from . import intercom_client
from . import org_vault
from . import ticket_ingest
from .intercom_oauth import PROVIDER

log = logging.getLogger("callproof.intercom")

_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "blockquote", "h1", "h2", "h3"}


class _HtmlToText(HTMLParser):
    def __init__(self):
        super().__init__()
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        self._parts.append(data)

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")


def _html_to_text(raw: str | None) -> str:
    """Intercom message bodies are HTML (e.g. "<p>Hey there!</p>"). Strip
    tags to plain text — evidence-quote validation matches against exact
    transcript text, so stray markup would break every quote check."""
    if not raw:
        return ""
    parser = _HtmlToText()
    try:
        parser.feed(raw)
    except Exception:  # noqa: BLE001
        return re.sub(r"<[^>]+>", " ", raw).strip()
    text = "".join(parser._parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _speaker_role(author_type: str | None) -> str:
    t = (author_type or "").strip().lower()
    if t == "bot":
        return "bot"
    if t in ("admin", "team"):
        return "agent"
    return "customer"


def _speaker_name(author: dict) -> str:
    return str(author.get("email") or author.get("name") or "").strip()


def _sent_at(unix_ts) -> datetime | None:
    if not isinstance(unix_ts, (int, float)) or unix_ts <= 0:
        return None
    try:
        return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def normalize_conversation(conversation: dict) -> list[dict]:
    """Conversation object (from GET /conversations/{id}) -> canonical
    turn list. Empty-text parts (workflow/system events with no message —
    assignment, snooze, close, etc.) are skipped; they have nothing for a
    rubric to score. conversation_summary parts are skipped outright, per
    the PRD: Intercom's own neutral recap is not a stand-in for CallLoop's
    evaluative audit_summary (IN-12), so it doesn't belong in the scored
    transcript either.
    """
    turns: list[dict] = []
    seq = 0

    source = conversation.get("source") or {}
    source_author = source.get("author") or {}
    source_text = _html_to_text(source.get("body"))
    if source_text and source_author:
        turns.append({
            "seq": seq,
            "speaker": _speaker_role(source_author.get("type")),
            "speaker_name": _speaker_name(source_author),
            "agent_user_id": None,
            "text": source_text,
            "sent_at": _sent_at(conversation.get("created_at")),
        })
        seq += 1

    parts_container = conversation.get("conversation_parts") or {}
    parts = parts_container.get("conversation_parts") or []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("part_type") == "conversation_summary":
            continue
        author = part.get("author") or {}
        text = _html_to_text(part.get("body"))
        if not text or not author:
            continue
        turns.append({
            "seq": seq,
            "speaker": _speaker_role(author.get("type")),
            "speaker_name": _speaker_name(author),
            "agent_user_id": None,
            "text": text,
            "sent_at": _sent_at(part.get("created_at")),
        })
        seq += 1

    return turns


def ingest_intercom_conversation(org_id: str, conversation_id: str) -> str:
    """Full write path for one closed Intercom conversation: dedup check,
    fetch, normalize, write to tickets/ticket_messages, mark ready.
    Idempotent — re-processing the same conversation_id (Intercom retries
    webhook delivery on any non-2xx/timeout) returns the existing ticket_id
    without creating a duplicate. Any failure marks the ticket 'failed' and
    re-raises, matching ingest_ticket_pdf()'s contract exactly.
    """
    existing = ticket_ingest.find_ticket_by_external_id(
        org_id, source="intercom_api", external_id=conversation_id,
    )
    if existing:
        applog.event(
            log, "intercom_ingest", result="deduped",
            org_id=org_id, conversation_id=conversation_id, ticket_id=existing,
        )
        return existing

    creds = org_vault.load_credential(org_id, PROVIDER)
    if not creds or not creds.get("access_token"):
        raise RuntimeError("Intercom is not connected for this org.")

    ticket_id = ticket_ingest.create_ticket(
        org_id, source="intercom_api", external_id=conversation_id,
    )
    ticket_ingest.set_ticket_status(ticket_id, org_id, "processing")
    try:
        conversation = intercom_client.get_conversation(creds["access_token"], conversation_id)
        turns = normalize_conversation(conversation)
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
    except Exception:
        ticket_ingest.set_ticket_status(ticket_id, org_id, "failed")
        raise
    ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")
    applog.event(
        log, "intercom_ingest", result="ok",
        org_id=org_id, conversation_id=conversation_id, ticket_id=ticket_id,
        turns=len(turns),
    )
    return ticket_id
