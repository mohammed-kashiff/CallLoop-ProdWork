"""IN-5/IN-7 (conversation half): normalizing a fetched Intercom
conversation into the canonical ticket-turn shape, and the ingest write
path's dedup/failure behavior. No live Intercom API calls, no real DB."""

from __future__ import annotations

import pytest

from backend import intercom_ingest


# ── _html_to_text ────────────────────────────────────────────────────────────


def test_html_to_text_strips_tags():
    assert intercom_ingest._html_to_text("<p>Hey there!</p>") == "Hey there!"


def test_html_to_text_converts_block_tags_to_newlines():
    html = "<p>Line one</p><p>Line two</p>"
    text = intercom_ingest._html_to_text(html)
    assert "Line one" in text and "Line two" in text
    assert text.index("Line one") < text.index("Line two")
    assert "Line one\nLine two" in text or "Line one\n\nLine two" in text


def test_html_to_text_handles_empty_and_none():
    assert intercom_ingest._html_to_text(None) == ""
    assert intercom_ingest._html_to_text("") == ""
    assert intercom_ingest._html_to_text("   ") == ""


# ── _speaker_role ─────────────────────────────────────────────────────────────


def test_speaker_role_maps_bot():
    assert intercom_ingest._speaker_role("bot") == "bot"


def test_speaker_role_maps_admin_and_team_to_agent():
    assert intercom_ingest._speaker_role("admin") == "agent"
    assert intercom_ingest._speaker_role("team") == "agent"


def test_speaker_role_defaults_unknown_to_customer():
    assert intercom_ingest._speaker_role("user") == "customer"
    assert intercom_ingest._speaker_role(None) == "customer"
    assert intercom_ingest._speaker_role("") == "customer"


# ── normalize_conversation ────────────────────────────────────────────────────

REAL_SHAPED_CONVERSATION = {
    "created_at": 1788900000,
    "source": {
        "type": "conversation",
        "delivered_as": "contact",
        "body": "<p>Dashboard is buggy, getting error messages.</p>",
        "author": {"type": "user", "email": "anthony@example.com", "name": "Anthony Brunetti"},
    },
    "conversation_parts": {
        "type": "conversation_part.list",
        "total_count": 3,
        "conversation_parts": [
            {
                "part_type": "comment",
                "created_at": 1788900100,
                "body": "<p>Got it — dashboard errors are often browser-related.</p>",
                "author": {"type": "bot", "name": "Welma Bot"},
            },
            {
                "part_type": "assignment",
                "created_at": 1788900150,
                "body": None,
                "author": {"type": "admin", "email": "kashif@intercom.example", "name": "Kashif"},
            },
            {
                "part_type": "comment",
                "created_at": 1788900200,
                "body": "<p>Thanks for reporting this, Anthony.</p>",
                "author": {"type": "admin", "email": "kashif@intercom.example", "name": "Kashif"},
            },
        ],
    },
}


def test_normalize_conversation_includes_the_opening_source_message():
    """The bug this test locks in: conversation_parts is only the replies
    AFTER the first message — source is a separate object. Skipping it
    would silently drop every conversation's opening line."""
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    assert turns[0]["speaker"] == "customer"
    assert turns[0]["speaker_name"] == "anthony@example.com"
    assert "Dashboard is buggy" in turns[0]["text"]
    assert turns[0]["seq"] == 0


def test_normalize_conversation_skips_empty_body_parts():
    """The assignment event has no body — nothing for a rubric to score."""
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    texts = [t["text"] for t in turns]
    assert not any(t == "" for t in texts)
    assert len(turns) == 3  # source + 2 comments, assignment skipped


def test_normalize_conversation_classifies_bot_and_agent_correctly():
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    roles = {t["speaker_name"]: t["speaker"] for t in turns if t["speaker_name"]}
    assert roles["Welma Bot"] == "bot"
    assert roles["kashif@intercom.example"] == "agent"


def test_normalize_conversation_seq_is_contiguous_and_ordered():
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    assert [t["seq"] for t in turns] == list(range(len(turns)))


def test_normalize_conversation_agent_user_id_always_none():
    """IN-10 (email-keyed identity resolution) isn't built yet — same
    honest gap ticket_pdf_parser already documents for PDF tickets."""
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    assert all(t["agent_user_id"] is None for t in turns)


def test_normalize_conversation_skips_conversation_summary_parts():
    convo = {
        "created_at": 1788900000,
        "source": {"body": "<p>hi</p>", "author": {"type": "user", "email": "a@b.com"}},
        "conversation_parts": {
            "conversation_parts": [
                {
                    "part_type": "conversation_summary",
                    "body": "<p>Customer asked about billing; agent resolved it.</p>",
                    "author": {"type": "admin", "email": "x@y.com"},
                },
            ],
        },
    }
    turns = intercom_ingest.normalize_conversation(convo)
    assert len(turns) == 1  # only the source turn — summary is never a turn
    assert "billing" not in turns[0]["text"]


def test_normalize_conversation_handles_missing_source_and_parts():
    assert intercom_ingest.normalize_conversation({}) == []


# ── ingest_intercom_conversation ──────────────────────────────────────────────


def test_ingest_intercom_conversation_dedupes_via_external_id(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda org_id, *, source, external_id: "existing-ticket-id",
    )
    fetch_called = []
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation",
        lambda token, cid: fetch_called.append(cid) or {},
    )
    result = intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert result == "existing-ticket-id"
    assert fetch_called == []  # never re-fetched — dedup short-circuits before any API call


def test_ingest_intercom_conversation_requires_a_stored_token(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(intercom_ingest.org_vault, "load_credential", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="not connected"):
        intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")


def test_ingest_intercom_conversation_marks_failed_on_fetch_error(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: "new-ticket-id",
    )
    statuses = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_status",
        lambda ticket_id, org_id, status: statuses.append(status),
    )

    def _boom(token, cid):
        raise RuntimeError("intercom is down")

    monkeypatch.setattr(intercom_ingest.intercom_client, "get_conversation", _boom)
    with pytest.raises(RuntimeError, match="intercom is down"):
        intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert statuses == ["processing", "failed"]


def test_ingest_intercom_conversation_happy_path_writes_turns_and_marks_ready(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: "new-ticket-id",
    )
    statuses = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_status",
        lambda ticket_id, org_id, status: statuses.append(status),
    )
    inserted = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages",
        lambda ticket_id, org_id, turns: inserted.update(ticket_id=ticket_id, turns=turns),
    )
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation",
        lambda token, cid: REAL_SHAPED_CONVERSATION,
    )

    result = intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert result == "new-ticket-id"
    assert statuses == ["processing", "ready"]
    assert inserted["ticket_id"] == "new-ticket-id"
    assert len(inserted["turns"]) == 3


# ── _external_id namespacing ──────────────────────────────────────────────────


def test_external_id_namespaces_by_kind():
    """A conversation and a ticket sharing the same raw Intercom id must
    never collide — both land in the same tickets.source='intercom_api'
    bucket, and Intercom doesn't document conversation/ticket ids as
    distinct spaces."""
    assert intercom_ingest._external_id("conversation", "123") == "conversation:123"
    assert intercom_ingest._external_id("ticket", "123") == "ticket:123"
    assert (
        intercom_ingest._external_id("conversation", "123")
        != intercom_ingest._external_id("ticket", "123")
    )


def test_ingest_intercom_conversation_uses_the_namespaced_external_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda org_id, *, source, external_id: seen.setdefault("dedup", external_id) and None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: seen.setdefault("create", external_id) and "t1",
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation", lambda *a, **k: {},
    )
    intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert seen["dedup"] == "conversation:conv-123"
    assert seen["create"] == "conversation:conv-123"


def test_ingest_intercom_ticket_uses_the_namespaced_external_id(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda org_id, *, source, external_id: seen.setdefault("dedup", external_id) and None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: seen.setdefault("create", external_id) and "t1",
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    monkeypatch.setattr(intercom_ingest.intercom_client, "get_ticket", lambda *a, **k: {})
    intercom_ingest.ingest_intercom_ticket("org-1", "ticket-456")
    assert seen["dedup"] == "ticket:ticket-456"
    assert seen["create"] == "ticket:ticket-456"


# ── normalize_ticket ──────────────────────────────────────────────────────────

REAL_SHAPED_TICKET = {
    "created_at": 1788900000,
    "ticket_attributes": {
        "_default_title_": "Dashboard is buggy",
        "_default_description_": "<p>Getting error messages on the dashboard.</p>",
    },
    "contacts": {"contacts": [{"email": "anthony@example.com", "name": "Anthony Brunetti"}]},
    "ticket_parts": {
        "ticket_parts": [
            {
                "part_type": "comment",
                "created_at": 1788900100,
                "body": "<p>We're looking into this now.</p>",
                "author": {"type": "admin", "email": "kashif@intercom.example", "name": "Kashif"},
            },
            {
                "part_type": "assignment",
                "created_at": 1788900150,
                "body": None,
                "author": {"type": "admin", "email": "kashif@intercom.example", "name": "Kashif"},
            },
        ],
    },
}


def test_normalize_ticket_includes_the_description_as_the_opening_turn():
    """A ticket has no `source` object like a conversation — its opening
    content is ticket_attributes._default_description_, attributed to the
    requester (contacts[0]), not an agent."""
    turns = intercom_ingest.normalize_ticket(REAL_SHAPED_TICKET)
    assert turns[0]["speaker"] == "customer"
    assert turns[0]["speaker_name"] == "anthony@example.com"
    assert "error messages" in turns[0]["text"]
    assert turns[0]["seq"] == 0


def test_normalize_ticket_includes_ticket_parts_and_skips_empty_ones():
    turns = intercom_ingest.normalize_ticket(REAL_SHAPED_TICKET)
    assert len(turns) == 2  # description + one real comment; assignment has no body
    assert turns[1]["speaker"] == "agent"
    assert "looking into this" in turns[1]["text"]


def test_normalize_ticket_seq_is_contiguous():
    turns = intercom_ingest.normalize_ticket(REAL_SHAPED_TICKET)
    assert [t["seq"] for t in turns] == list(range(len(turns)))


def test_normalize_ticket_agent_user_id_always_none():
    turns = intercom_ingest.normalize_ticket(REAL_SHAPED_TICKET)
    assert all(t["agent_user_id"] is None for t in turns)


def test_normalize_ticket_handles_missing_description_and_parts():
    assert intercom_ingest.normalize_ticket({}) == []


def test_normalize_ticket_handles_a_flat_ticket_parts_list():
    """ticket_parts' container nesting isn't confirmed the way
    conversation_parts' was — must accept a flat list too, not just the
    conversation-style nested container, without raising."""
    ticket = {
        "ticket_attributes": {},
        "ticket_parts": [
            {
                "part_type": "comment",
                "body": "<p>Flat shape reply.</p>",
                "author": {"type": "admin", "email": "a@b.com"},
                "created_at": 1788900000,
            },
        ],
    }
    turns = intercom_ingest.normalize_ticket(ticket)
    assert len(turns) == 1
    assert "Flat shape reply" in turns[0]["text"]


def test_normalize_ticket_handles_no_contacts_gracefully():
    ticket = {
        "ticket_attributes": {"_default_description_": "<p>hi</p>"},
        "contacts": {"contacts": []},
    }
    turns = intercom_ingest.normalize_ticket(ticket)
    assert len(turns) == 1
    assert turns[0]["speaker_name"] == ""


# ── ingest_intercom_ticket ─────────────────────────────────────────────────────


def test_ingest_intercom_ticket_dedupes(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda org_id, *, source, external_id: "existing-ticket-id",
    )
    fetch_called = []
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_ticket",
        lambda token, tid: fetch_called.append(tid) or {},
    )
    result = intercom_ingest.ingest_intercom_ticket("org-1", "ticket-1")
    assert result == "existing-ticket-id"
    assert fetch_called == []


def test_ingest_intercom_ticket_requires_a_stored_token(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(intercom_ingest.org_vault, "load_credential", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="not connected"):
        intercom_ingest.ingest_intercom_ticket("org-1", "ticket-1")


def test_ingest_intercom_ticket_happy_path(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: "new-ticket-id",
    )
    statuses = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_status",
        lambda ticket_id, org_id, status: statuses.append(status),
    )
    inserted = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages",
        lambda ticket_id, org_id, turns: inserted.update(turns=turns),
    )
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_ticket", lambda token, tid: REAL_SHAPED_TICKET,
    )
    result = intercom_ingest.ingest_intercom_ticket("org-1", "ticket-1")
    assert result == "new-ticket-id"
    assert statuses == ["processing", "ready"]
    assert len(inserted["turns"]) == 2


def test_ingest_intercom_ticket_marks_failed_on_fetch_error(monkeypatch):
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: "new-ticket-id",
    )
    statuses = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_status",
        lambda ticket_id, org_id, status: statuses.append(status),
    )

    def _boom(token, tid):
        raise RuntimeError("intercom is down")

    monkeypatch.setattr(intercom_ingest.intercom_client, "get_ticket", _boom)
    with pytest.raises(RuntimeError, match="intercom is down"):
        intercom_ingest.ingest_intercom_ticket("org-1", "ticket-1")
    assert statuses == ["processing", "failed"]
