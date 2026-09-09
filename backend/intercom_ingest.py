"""
CallProof - Intercom conversation + ticket ingest (IN-5/IN-6/IN-7/IN-8/IN-9/IN-10).

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
  - `statistics` (IN-7: first-response/resolution timing) exists only on
    the Conversation object, not Ticket — confirmed against Intercom's
    own reference before assuming otherwise, the same discipline that
    caught the state-vs-open ticket-search bug (IN-6). Stored as-is,
    unparsed, in tickets.provider_stats (0032) — v1, not surfaced yet.
  - IN-8 (linked_objects grouping): the epic's own written "resolved
    logic" (a `ticket` field on Conversation, null/non-null) does not
    match Intercom's actual schema — checked directly against Intercom's
    published OpenAPI spec (github.com/intercom/Intercom-OpenAPI), both
    the 2.14 this client requests and current 2.16. There is no `ticket`
    field on Conversation in either version. What both versions actually
    have: Conversation *and* Ticket each carry their own `linked_objects`
    field directly (same `linked_object_list` shape on both —
    {type, total_count, has_more, data: [{type, id, category}]}), up to
    1000 entries. Grouping logic here is built against that verified
    shape, not the epic's stated one. Also confirmed while in the spec:
    Ticket's `ticket_id` field (distinct from `id`) is the UI-facing
    display number ("Do not use ticket_id for API queries" — Intercom's
    own words) — explains a `#131722754`-style number seen in Intercom's
    UI never matching the numeric id this code actually fetches by.

external_id is namespaced by object kind ("conversation:<id>" /
"ticket:<id>") for a standalone object — Intercom's conversation and
ticket ids aren't documented as distinct id-spaces, and both land in the
same `tickets.source = "intercom_api"` bucket, so an unprefixed id risks
one silently deduping against the other. A *grouped* case (IN-8, a
non-empty linked_objects) instead gets "group:<lowest numeric id in the
group>" — deterministic regardless of which member is discovered first
(a webhook on the conversation vs. the poller independently finding the
linked ticket), so both entry points converge on the same row without
needing a separate lookup table. Single-hop only: a member's own
linked_objects aren't recursively expanded, matching the epic's stated
acceptance criterion (merge a ticket's own linked_objects, not a
transitive closure over the whole graph) — undocumented and unbuilt if
Intercom's linking graph ever isn't a fully-connected single hop.

agent_user_id (IN-10): resolved at the end of _ingest_intercom_object,
batched (one query per ingest, mirroring ticket_ingest.ingest_ticket_pdf's
exact pattern for the PDF path) via ticket_agent_identity_aliases —
built on its own schema (0034), a table parallel to TA-15's
ticket_agent_aliases rather than a retrofit of it (see that module's
docstring for why: display_name there is a freeform PDF name, a
genuinely different identifier kind from Intercom's structured email).
An agent turn's speaker_name IS already its email (_speaker_name()
prefers author.email over author.name), so no separate raw-identifier
field is needed the way agent_display_name is for the PDF path — the
existing turn shape already carries the lookup key.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from . import applog
from . import intercom_client
from . import org_vault
from . import ticket_agent_identity_aliases
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


def _is_internal_note(part_type: str) -> bool:
    """IN-9: Intercom's part_type isn't a documented closed enum (its own
    OpenAPI spec types it as a bare string, no enum list) — the one
    confirmed real value is "note_and_unsnooze" (a real sample carried a
    decisive, customer-impacting decision through exactly this part,
    never surfaced as a customer-facing comment — the original "discard
    all notes" design was wrong because of this). Matched by prefix
    rather than an exhaustive literal list, so an unseen sibling
    ("note_and_reopen", "note_and_close", etc.) is still caught rather
    than silently mis-tagged as customer-facing."""
    return part_type == "note" or part_type.startswith("note_and_")


def _turn_from_part(part: dict, seq: int) -> dict | None:
    """A conversation_part and a ticket_part share the exact same shape
    per Intercom's schema (part_type/author/body/created_at) — one
    extractor covers both. Returns None for parts with no scoreable text
    (workflow/system events — assignment, snooze, close, etc. — or
    conversation_summary, which the PRD explicitly says is a neutral
    recap, not a stand-in for CallLoop's own evaluative audit_summary).

    internal_contribution (IN-9) tags a note-type part — captured, not
    discarded, but excluded from scoring dimensions that specifically
    judge customer-facing communication (see ticket_rubric.py's
    customer_facing_only flag and ticket_scoring.run_ticket_wave)."""
    if not isinstance(part, dict):
        return None
    part_type = str(part.get("part_type") or "")
    if part_type == "conversation_summary":
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
        "internal_contribution": _is_internal_note(part_type),
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
            "internal_contribution": False,
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
            "internal_contribution": False,
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
    tickets.source = "intercom_api" bucket. Only for a standalone object;
    a grouped case (IN-8) uses _group_key() instead."""
    return f"{kind}:{raw_id}"


def _linked_members(obj: dict) -> list[tuple[str, str]]:
    """(kind, id) pairs from an object's own linked_objects.data (IN-8),
    excluding any malformed entry, a duplicate id, and a self-reference.
    Single-hop only — a member's own linked_objects isn't followed."""
    raw = (obj.get("linked_objects") or {}).get("data") or []
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("type")
        oid = str(entry.get("id") or "").strip()
        if kind not in ("conversation", "ticket") or not oid or oid in seen:
            continue
        seen.add(oid)
        out.append((kind, oid))
    return out


def _numeric_sort_key(raw_id: str):
    """Compares Intercom ids numerically, not lexicographically (a plain
    string min() would rank "9" ahead of "215475853780573"). Falls back
    to string comparison, sorted after every numeric id, for the
    undocumented case of a non-numeric id."""
    try:
        return (0, int(raw_id))
    except (TypeError, ValueError):
        return (1, raw_id)


def _group_key(member_ids: list[str]) -> str:
    """IN-8: the dedup key for a grouped case — the lowest id across every
    member (the seed object plus everything in its linked_objects),
    namespaced separately from a standalone object's kind-prefixed key.
    Deterministic regardless of which member is discovered first (a
    webhook on the conversation half vs. the poller independently finding
    the linked ticket) — both converge on the same key without needing a
    separate lookup table."""
    lowest = min(member_ids, key=_numeric_sort_key)
    return f"group:{lowest}"


def _fetch_member(access_token: str, kind: str, obj_id: str) -> dict:
    if kind == "conversation":
        return intercom_client.get_conversation(access_token, obj_id)
    return intercom_client.get_ticket(access_token, obj_id)


def _normalize_member(kind: str, obj: dict) -> list[dict]:
    if kind == "conversation":
        return normalize_conversation(obj)
    return normalize_ticket(obj)


def _merge_turns(turn_lists: list[list[dict]]) -> list[dict]:
    """IN-8: flattens every member's turns into one chronological sequence
    and re-sequences seq 0..N-1. Sorts by sent_at; Python's sort is
    stable, so a turn with no sent_at (or a tie) keeps its original
    position — seed member's turns first, then each linked member in
    linked_objects.data order — a reasonable deterministic default absent
    a real timestamp to order by."""
    combined = [t for turns in turn_lists for t in turns]
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    combined.sort(key=lambda t: (t.get("sent_at") is None, t.get("sent_at") or epoch))
    for i, t in enumerate(combined):
        t["seq"] = i
    return combined


def _resolve_agent_identities(org_id: str, turns: list[dict]) -> None:
    """IN-10: mutates every agent turn's agent_user_id in place, batched
    — one query per ingest rather than one per turn, the same pattern
    ingest_ticket_pdf() already uses for the PDF path's display-name
    resolution. An agent turn's speaker_name is already its email
    (_speaker_name() prefers author.email over author.name); this just
    resolves it against ticket_agent_identity_aliases instead of always
    leaving it None. No-op, not an error, for an org with no aliases
    configured yet — those turns simply stay unresolved."""
    unresolved = {
        t["speaker_name"] for t in turns
        if t["speaker"] == "agent" and not t.get("agent_user_id") and t.get("speaker_name")
    }
    if not unresolved:
        return
    resolved = ticket_agent_identity_aliases.resolve_agent_user_ids(org_id, PROVIDER, unresolved)
    if not resolved:
        return
    for t in turns:
        if t["speaker"] == "agent" and not t.get("agent_user_id"):
            uid = resolved.get(t["speaker_name"].strip().lower())
            if uid:
                t["agent_user_id"] = uid


def _ingest_intercom_object(org_id: str, kind: str, obj_id: str) -> str:
    """Shared write path for both ingest_intercom_conversation and
    ingest_intercom_ticket. Two dedup checks, in order:

    1. By this object's own kind-prefixed key — cheap, catches a retried
       webhook/poll for an object already known to be standalone, with no
       fetch needed.
    2. If step 1 misses, fetch the object anyway (needed regardless, to
       learn its linked_objects) and, only if it turns out to belong to a
       group, check the group's key instead — catches the case where this
       exact object was already ingested as part of a group under a
       different id's key. Costs one "wasted" fetch on an already-known
       group member; never creates a duplicate row.

    The ticket row itself isn't created until after this object's own
    fetch succeeds and its real dedup key (seed or group) is known — a
    deliberate change from the pre-IN-8 shape, where a row was created
    before any fetch. Creating it earlier would risk a wrongly-keyed row
    for an object that turns out to belong to an already-ingested group.
    A failure fetching THIS object therefore creates no row at all (still
    fully visible via the intercom_ingest_failed log line the api.py call
    site already emits on any exception here) — but once the row exists,
    a failure fetching a linked member or writing turns still marks it
    'failed' and re-raises, same contract as ingest_ticket_pdf().
    """
    seed_ext_id = _external_id(kind, obj_id)
    existing = ticket_ingest.find_ticket_by_external_id(
        org_id, source="intercom_api", external_id=seed_ext_id,
    )
    if existing:
        applog.event(
            log, "intercom_ingest", result="deduped", kind=kind,
            org_id=org_id, intercom_id=obj_id, ticket_id=existing,
        )
        return existing

    creds = org_vault.load_credential(org_id, PROVIDER)
    if not creds or not creds.get("access_token"):
        raise RuntimeError("Intercom is not connected for this org.")
    access_token = creds["access_token"]

    seed_obj = _fetch_member(access_token, kind, obj_id)
    linked = _linked_members(seed_obj)

    if not linked:
        ext_id = seed_ext_id
    else:
        ext_id = _group_key([obj_id] + [oid for _, oid in linked])
        existing_group = ticket_ingest.find_ticket_by_external_id(
            org_id, source="intercom_api", external_id=ext_id,
        )
        if existing_group:
            applog.event(
                log, "intercom_ingest", result="deduped", kind="group",
                org_id=org_id, intercom_id=obj_id, ticket_id=existing_group,
            )
            return existing_group

    ticket_id = ticket_ingest.create_ticket(org_id, source="intercom_api", external_id=ext_id)
    ticket_ingest.set_ticket_status(ticket_id, org_id, "processing")
    try:
        members: list[tuple[str, str, dict]] = [(kind, obj_id, seed_obj)]
        for member_kind, member_id in linked:
            members.append((member_kind, member_id, _fetch_member(access_token, member_kind, member_id)))
        turns = _merge_turns([_normalize_member(k, o) for k, _, o in members])
        _resolve_agent_identities(org_id, turns)
        ticket_ingest.insert_ticket_messages(ticket_id, org_id, turns)
        # statistics (IN-7) only exists on Conversation, and only means
        # something unambiguous for a lone conversation — a grouped case
        # has no single obvious "the" statistics object, so it's skipped
        # rather than guessed at (v1, unsurfaced field either way).
        if len(members) == 1 and kind == "conversation":
            ticket_ingest.set_ticket_provider_stats(ticket_id, org_id, seed_obj.get("statistics"))
    except Exception:
        ticket_ingest.set_ticket_status(ticket_id, org_id, "failed")
        raise
    ticket_ingest.set_ticket_status(ticket_id, org_id, "ready")
    applog.event(
        log, "intercom_ingest", result="ok",
        kind=("group" if len(members) > 1 else kind),
        org_id=org_id, intercom_id=obj_id, ticket_id=ticket_id,
        turns=len(turns), group_size=len(members),
    )
    return ticket_id


def ingest_intercom_conversation(org_id: str, conversation_id: str) -> str:
    """Full write path for one closed Intercom conversation. Idempotent —
    re-processing the same conversation_id (Intercom retries webhook
    delivery on any non-2xx/timeout) returns the existing ticket_id
    without creating a duplicate. IN-8: a conversation with a non-empty
    linked_objects merges with every linked member into one case instead
    of ingesting standalone — see _ingest_intercom_object."""
    return _ingest_intercom_object(org_id, "conversation", conversation_id)


def ingest_intercom_ticket(org_id: str, ticket_id_intercom: str) -> str:
    """Full write path for one closed/resolved Intercom ticket — same
    contract and same IN-8 grouping behavior as ingest_intercom_conversation,
    against GET /tickets/{id} and normalize_ticket() instead."""
    return _ingest_intercom_object(org_id, "ticket", ticket_id_intercom)
