"""
CallProof - Intercom conversation + ticket ingest (IN-5/IN-6/IN-7).

Normalizes a fetched Intercom Conversation or Ticket object into the same
canonical turn shape ticket_pdf_parser.parse_turns() produces — {seq,
speaker, speaker_name, agent_user_id, text, sent_at} — so ticket_ingest.py's
existing DB-writing functions and the scoring engine need zero changes to
handle an Intercom-sourced ticket alongside a PDF-sourced one.

Every field mapping here is checked against Intercom's published API
reference before being written, the same discipline the original PRD used
against real sample payloads — this codebase just doesn't have a real
ticket-first customer sample to verify against yet, so "checked against
Intercom's own docs" is the honest substitute, not a guess.

Things confirmed here that the PRD's own field-mapping table (built from
two real payloads, apparently not exhaustive) didn't cover:
  - A conversation's opening message lives in `source`, a separate object
    from `conversation_parts` — conversation_parts is only the replies/
    notes that came AFTER the first message. Skipping `source` would
    silently drop every conversation's opening line.
  - The parts array is nested: `conversation_parts.conversation_parts`,
    not a flat top-level `conversation_parts` list.
  - A Ticket's opening content isn't a `source` object like a
    conversation's — it's the `_default_description_` key inside
    `ticket_attributes` (a plain string, no author of its own), authored
    by the ticket's requester (`contacts`, first entry).
  - `ticket_parts` share the exact same part shape as `conversation_parts`
    (part_type/author/body/created_at) per Intercom's own schema, so the
    same turn-extraction logic (_turn_from_part) covers both.
  - `ticket.resolved` and `ticket.closed` webhooks put the ticket object
    in different places (top-level `data.item` vs. nested
    `data.item.ticket`) — handled at the call site (api.py), not here.

external_id is namespaced by object kind ("conversation:<id>" /
"ticket:<id>") — Intercom's conversation and ticket ids aren't documented
as distinct id-spaces, and both land in the same `tickets.source =
"intercom_api"` bucket, so an unprefixed id risks one silently deduping
against the other.

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


def _turn_from_part(part: dict, seq: int) -> dict | None:
    """A conversation_part and a ticket_part share the exact same shape
    per Intercom's schema (part_type/author/body/created_at) — one
    extractor covers both. Returns None for parts with no scoreable text
    (workflow/system events — assignment, snooze, close, etc. — or
    conversation_summary, which the PRD explicitly says is a neutral
    recap, not a stand-in for CallLoop's own evaluative audit_summary)."""
    if not isinstance(part, dict):
        return None
    if part.get("part_type") == "conversation_summary":
        return None
    author = part.get("author") or {}
    text = _html_to_text(part.get("body"))
    if not text or not author:
        return None
    return {
        "seq": seq,
        "speaker": _speaker_role(author.get("type")),
        "speaker_name": _speaker_name(author),
        "agent_user_id": None,
        "text": text,
        "sent_at": _sent_at(part.get("created_at")),
    }


def normalize_conversation(conversation: dict) -> list[dict]:
    """Conversation object (from GET /conversations/{id}) -> canonical
    turn list."""
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
        turn = _turn_from_part(part, seq)
        if turn is not None:
            turns.append(turn)
            seq += 1

    return turns


def _ticket_parts_list(ticket: dict) -> list:
    """ticket_parts' own container shape isn't confirmed the same way
    conversation_parts' nesting was (no real ticket-first sample to check
    against) — accept either a flat list or a conversation-style nested
    {"ticket_parts": [...]} container, so a wrong guess here fails soft
    (falls through to an empty list) rather than raising."""
    raw = ticket.get("ticket_parts")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        nested = raw.get("ticket_parts")
        if isinstance(nested, list):
            return nested
    return []


def normalize_ticket(ticket: dict) -> list[dict]:
    """Ticket object (from GET /tickets/{id}) -> canonical turn list.

    A ticket has no `source` object the way a conversation does — its
    opening content is ticket_attributes["_default_description_"], a
    plain string with no author of its own. Attributed to the ticket's
    requester (contacts[0]) as a customer turn, since a ticket's
    description is what the requester submitted, not agent-authored.
    """
    turns: list[dict] = []
    seq = 0

    attrs = ticket.get("ticket_attributes") or {}
    description = _html_to_text(attrs.get("_default_description_"))
    if description:
        contacts = ticket.get("contacts") or {}
        contact_list = contacts.get("contacts") if isinstance(contacts, dict) else contacts
        first_contact = (contact_list or [{}])[0] if contact_list else {}
        turns.append({
            "seq": seq,
            "speaker": "customer",
            "speaker_name": _speaker_name(first_contact if isinstance(first_contact, dict) else {}),
            "agent_user_id": None,
            "text": description,
            "sent_at": _sent_at(ticket.get("created_at")),
        })
        seq += 1

    for part in _ticket_parts_list(ticket):
        turn = _turn_from_part(part, seq)
        if turn is not None:
            turns.append(turn)
            seq += 1

    return turns


def _external_id(kind: str, raw_id: str) -> str:
    """Namespaced so a conversation and a ticket sharing the same raw
    Intercom id (their id-spaces aren't documented as distinct) can never
    silently dedupe against each other — both land in the same
    tickets.source = "intercom_api" bucket."""
    return f"{kind}:{raw_id}"


def ingest_intercom_conversation(org_id: str, conversation_id: str) -> str:
    """Full write path for one closed Intercom conversation: dedup check,
    fetch, normalize, write to tickets/ticket_messages, mark ready.
    Idempotent — re-processing the same conversation_id (Intercom retries
    webhook delivery on any non-2xx/timeout) returns the existing ticket_id
    without creating a duplicate. Any failure marks the ticket 'failed' and
    re-raises, matching ingest_ticket_pdf()'s contract exactly.
    """
    ext_id = _external_id("conversation", conversation_id)
    existing = ticket_ingest.find_ticket_by_external_id(
        org_id, source="intercom_api", external_id=ext_id,
    )
    if existing:
        applog.event(
            log, "intercom_ingest", result="deduped", kind="conversation",
            org_id=org_id, conversation_id=conversation_id, ticket_id=existing,
        )
        return existing

    creds = org_vault.load_credential(org_id, PROVIDER)
    if not creds or not creds.get("access_token"):
        raise RuntimeError("Intercom is not connected for this org.")

    ticket_id = ticket_ingest.create_ticket(
        org_id, source="intercom_api", external_id=ext_id,
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
        log, "intercom_ingest", result="ok", kind="conversation",
        org_id=org_id, conversation_id=conversation_id, ticket_id=ticket_id,
        turns=len(turns),
    )
    return ticket_id


def ingest_intercom_ticket(org_id: str, ticket_id_intercom: str) -> str:
    """Full write path for one closed/resolved Intercom ticket — same
    contract as ingest_intercom_conversation (dedup, fetch, normalize,
    write, mark ready/failed), against GET /tickets/{id} and
    normalize_ticket() instead. Does not attempt linked_objects
    grouping — a ticket ingests as its own single case here; merging
    multiple linked conversations/tickets into one audited case is IN-8,
    against a real ticket-first sample.
    """
    ext_id = _external_id("ticket", ticket_id_intercom)
    existing = ticket_ingest.find_ticket_by_external_id(
        org_id, source="intercom_api", external_id=ext_id,
    )
    if existing:
        applog.event(
            log, "intercom_ingest", result="deduped", kind="ticket",
            org_id=org_id, intercom_ticket_id=ticket_id_intercom, ticket_id=existing,
        )
        return existing

    creds = org_vault.load_credential(org_id, PROVIDER)
    if not creds or not creds.get("access_token"):
        raise RuntimeError("Intercom is not connected for this org.")

    ticket_id = ticket_ingest.create_ticket(
        org_id, source="intercom_api", external_id=ext_id,
    )
    ticket_ingest.set_ticket_status(ticket_id, org_id, "processing")
    try:
        ticket = intercom_client.get_ticket(creds["access_token"], ticket_id_intercom)
        turns = normalize_ticket(ticket)
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
    except Exception:
        ticket_ingest.set_ticket_status(ticket_id, org_id, "failed")
        raise
    ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")
    applog.event(
        log, "intercom_ingest", result="ok", kind="ticket",
        org_id=org_id, intercom_ticket_id=ticket_id_intercom, ticket_id=ticket_id,
        turns=len(turns),
    )
    return ticket_id
