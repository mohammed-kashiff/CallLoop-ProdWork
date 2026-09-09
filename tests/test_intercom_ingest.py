"""IN-5/IN-7 (conversation half): normalizing a fetched Intercom
conversation into the canonical ticket-turn shape, and the ingest write
path's dedup/failure behavior. No live Intercom API calls, no real DB."""

from __future__ import annotations

import pytest

from backend import intercom_ingest


@pytest.fixture(autouse=True)
def _no_agent_identity_resolution_by_default(monkeypatch):
    """IN-10: every ingest path now calls _resolve_agent_identities,
    which queries a real table via org_scope() — every test in this
    file uses a fake org_id ("org-1"), not a real UUID, so that would
    raise unless mocked. Default: no aliases configured, matching the
    real behavior for an org that hasn't set any up yet. Tests that
    specifically exercise resolution override this themselves."""
    monkeypatch.setattr(
        intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids",
        lambda *a, **k: {},
    )


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


# ── _is_internal_note (IN-9) ─────────────────────────────────────────────────


def test_is_internal_note_matches_the_one_confirmed_real_value():
    assert intercom_ingest._is_internal_note("note_and_unsnooze") is True


def test_is_internal_note_matches_bare_note_and_any_note_and_prefix():
    assert intercom_ingest._is_internal_note("note") is True
    assert intercom_ingest._is_internal_note("note_and_reopen") is True
    assert intercom_ingest._is_internal_note("note_and_close") is True


def test_is_internal_note_false_for_customer_facing_part_types():
    assert intercom_ingest._is_internal_note("comment") is False
    assert intercom_ingest._is_internal_note("assignment") is False
    assert intercom_ingest._is_internal_note("") is False


def test_turn_from_part_tags_a_note_as_internal():
    part = {
        "part_type": "note_and_unsnooze",
        "body": "<p>Refund approved per policy §6.2.</p>",
        "author": {"type": "admin", "email": "kashif@intercom.example"},
        "created_at": 1788900100,
    }
    turn = intercom_ingest._turn_from_part(part, 0)
    assert turn["internal_contribution"] is True
    assert "Refund approved" in turn["text"]


def test_turn_from_part_tags_a_comment_as_not_internal():
    part = {
        "part_type": "comment",
        "body": "<p>Thanks for reaching out!</p>",
        "author": {"type": "admin", "email": "kashif@intercom.example"},
        "created_at": 1788900100,
    }
    turn = intercom_ingest._turn_from_part(part, 0)
    assert turn["internal_contribution"] is False


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
    assert turns[0]["internal_contribution"] is False


def test_normalize_conversation_tags_a_note_part_as_internal():
    """IN-9: a note-type part is captured (not discarded) and tagged, not
    silently merged in as an ordinary agent turn."""
    convo = {
        "created_at": 1788900000,
        "source": {"body": "<p>hi</p>", "author": {"type": "user", "email": "a@b.com"}},
        "conversation_parts": {
            "conversation_parts": [
                {
                    "part_type": "note_and_unsnooze",
                    "body": "<p>Refund approved internally.</p>",
                    "author": {"type": "admin", "email": "kashif@intercom.example"},
                    "created_at": 1788900100,
                },
                {
                    "part_type": "comment",
                    "body": "<p>Thanks, all set!</p>",
                    "author": {"type": "admin", "email": "kashif@intercom.example"},
                    "created_at": 1788900200,
                },
            ],
        },
    }
    turns = intercom_ingest.normalize_conversation(convo)
    assert [t["internal_contribution"] for t in turns] == [False, True, False]


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


# ── _resolve_agent_identities (IN-10) ────────────────────────────────────────


def test_resolve_agent_identities_fills_in_agent_user_id(monkeypatch):
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    monkeypatch.setattr(
        intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids",
        lambda org_id, provider, idents: {"kashif@intercom.example": "u1"},
    )
    intercom_ingest._resolve_agent_identities("org-1", turns)
    agent_turns = [t for t in turns if t["speaker"] == "agent"]
    assert agent_turns and all(t["agent_user_id"] == "u1" for t in agent_turns)


def test_resolve_agent_identities_leaves_unmapped_turns_none(monkeypatch):
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    monkeypatch.setattr(
        intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids",
        lambda *a, **k: {},
    )
    intercom_ingest._resolve_agent_identities("org-1", turns)
    assert all(t["agent_user_id"] is None for t in turns if t["speaker"] == "agent")


def test_resolve_agent_identities_never_touches_non_agent_turns(monkeypatch):
    turns = intercom_ingest.normalize_conversation(REAL_SHAPED_CONVERSATION)
    monkeypatch.setattr(
        intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids",
        lambda *a, **k: {"anthony@example.com": "should-never-be-used", "welma bot": "nope"},
    )
    intercom_ingest._resolve_agent_identities("org-1", turns)
    assert all(
        t["agent_user_id"] is None for t in turns if t["speaker"] in ("customer", "bot")
    )


def test_resolve_agent_identities_matches_case_insensitively(monkeypatch):
    turns = [{"seq": 0, "speaker": "agent", "speaker_name": "Kashif@Intercom.Example",
              "agent_user_id": None, "text": "hi"}]
    captured = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids",
        lambda org_id, provider, idents: captured.setdefault("idents", idents)
        and {"kashif@intercom.example": "u1"},
    )
    intercom_ingest._resolve_agent_identities("org-1", turns)
    assert turns[0]["agent_user_id"] == "u1"


def test_resolve_agent_identities_short_circuits_when_nothing_unresolved(monkeypatch):
    turns = [{"seq": 0, "speaker": "agent", "speaker_name": "a@b.com", "agent_user_id": "already-set"}]

    def _boom(*a, **k):
        raise AssertionError("must not query when every agent turn is already resolved")

    monkeypatch.setattr(intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids", _boom)
    intercom_ingest._resolve_agent_identities("org-1", turns)
    assert turns[0]["agent_user_id"] == "already-set"


def test_resolve_agent_identities_passes_the_intercom_provider(monkeypatch):
    turns = [{"seq": 0, "speaker": "agent", "speaker_name": "a@b.com", "agent_user_id": None}]
    captured = {}

    def _fake(org_id, provider, idents):
        captured["provider"] = provider
        return {}

    monkeypatch.setattr(intercom_ingest.ticket_agent_identity_aliases, "resolve_agent_user_ids", _fake)
    intercom_ingest._resolve_agent_identities("org-1", turns)
    assert captured["provider"] == intercom_ingest.PROVIDER == "intercom"


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


def test_ingest_intercom_conversation_creates_no_row_when_seed_fetch_fails(monkeypatch):
    """IN-8: the seed object's fetch happens before create_ticket now (its
    linked_objects has to be known before the real dedup key can be
    computed) — a failure fetching it therefore creates no row at all.
    Still fully visible via the api.py call site's intercom_ingest_failed
    log line on any exception from this function."""
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id",
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: create_calls.append(external_id) or "new-ticket-id",
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
    assert create_calls == []
    assert statuses == []


def test_ingest_intercom_conversation_marks_failed_when_a_linked_member_fetch_fails(monkeypatch):
    """Once the row exists (the seed's own fetch succeeded, and grouping
    was resolved), a failure fetching a *linked* member still marks the
    row failed and re-raises — the pre-IN-8 contract, preserved for
    everything after the row is created."""
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
    convo_with_link = {
        **REAL_SHAPED_CONVERSATION,
        "linked_objects": {"data": [{"type": "ticket", "id": "t-999"}]},
    }
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation", lambda *a, **k: convo_with_link,
    )

    def _boom(token, tid):
        raise RuntimeError("intercom is down")

    monkeypatch.setattr(intercom_ingest.intercom_client, "get_ticket", _boom)
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


def test_ingest_intercom_conversation_stores_statistics_when_present(monkeypatch):
    """IN-7: conversation.statistics (first-response/resolution timing,
    Conversation-only field per Intercom's reference) is passed through
    to tickets.provider_stats as-is."""
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
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    stats = {"time_to_first_close": 3600, "count_reopens": 0}
    convo = {**REAL_SHAPED_CONVERSATION, "statistics": stats}
    monkeypatch.setattr(intercom_ingest.intercom_client, "get_conversation", lambda *a, **k: convo)

    captured = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_provider_stats",
        lambda ticket_id, org_id, s: captured.update(ticket_id=ticket_id, stats=s),
    )
    intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert captured == {"ticket_id": "new-ticket-id", "stats": stats}


def test_ingest_intercom_conversation_skips_statistics_write_when_absent(monkeypatch):
    """No `statistics` key on the payload — set_ticket_provider_stats is
    never even called, not called-with-None."""
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
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation",
        lambda *a, **k: REAL_SHAPED_CONVERSATION,
    )
    calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_provider_stats",
        lambda *a, **k: calls.append((a, k)),
    )
    intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert calls == [(("new-ticket-id", "org-1", None), {})]


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


# ── IN-8: linked_objects grouping ───────────────────────────────────────────────


def test_linked_members_extracts_type_and_id():
    obj = {"linked_objects": {"data": [
        {"type": "ticket", "id": "t1", "category": "Customer"},
        {"type": "conversation", "id": "c2"},
    ]}}
    assert intercom_ingest._linked_members(obj) == [("ticket", "t1"), ("conversation", "c2")]


def test_linked_members_handles_missing_or_empty_linked_objects():
    assert intercom_ingest._linked_members({}) == []
    assert intercom_ingest._linked_members({"linked_objects": {}}) == []
    assert intercom_ingest._linked_members({"linked_objects": {"data": []}}) == []


def test_linked_members_skips_malformed_and_unknown_type_entries():
    obj = {"linked_objects": {"data": [
        "not-a-dict",
        {"type": "ticket"},  # no id
        {"id": "x1"},  # no type
        {"type": "contact", "id": "c1"},  # not ticket/conversation
        {"type": "ticket", "id": "t1"},
    ]}}
    assert intercom_ingest._linked_members(obj) == [("ticket", "t1")]


def test_linked_members_dedupes_repeated_ids():
    obj = {"linked_objects": {"data": [
        {"type": "ticket", "id": "t1"},
        {"type": "ticket", "id": "t1"},
    ]}}
    assert intercom_ingest._linked_members(obj) == [("ticket", "t1")]


def test_numeric_sort_key_compares_numerically_not_lexicographically():
    ids = ["215475853780573", "9", "100"]
    assert min(ids, key=intercom_ingest._numeric_sort_key) == "9"


def test_numeric_sort_key_falls_back_to_string_for_non_numeric():
    # A non-numeric id sorts after every numeric one, never crashes.
    ids = ["215475853780573", "not-a-number"]
    assert min(ids, key=intercom_ingest._numeric_sort_key) == "215475853780573"


def test_group_key_picks_the_lowest_numeric_id_regardless_of_order():
    assert intercom_ingest._group_key(["215475853780573", "8"]) == "group:8"
    assert intercom_ingest._group_key(["8", "215475853780573"]) == "group:8"


def test_merge_turns_sorts_by_sent_at_and_resequences():
    from datetime import datetime, timezone

    later = {"seq": 0, "speaker": "agent", "speaker_name": "a", "agent_user_id": None,
              "text": "second", "sent_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    earlier = {"seq": 0, "speaker": "customer", "speaker_name": "c", "agent_user_id": None,
               "text": "first", "sent_at": datetime(2025, 1, 1, tzinfo=timezone.utc)}
    merged = intercom_ingest._merge_turns([[later], [earlier]])
    assert [t["text"] for t in merged] == ["first", "second"]
    assert [t["seq"] for t in merged] == [0, 1]


def test_merge_turns_keeps_original_order_for_missing_sent_at():
    a = {"seq": 0, "speaker": "customer", "speaker_name": "a", "agent_user_id": None,
         "text": "from seed", "sent_at": None}
    b = {"seq": 0, "speaker": "agent", "speaker_name": "b", "agent_user_id": None,
         "text": "from linked", "sent_at": None}
    merged = intercom_ingest._merge_turns([[a], [b]])
    assert [t["text"] for t in merged] == ["from seed", "from linked"]


def test_ingest_intercom_conversation_merges_a_linked_ticket_into_one_case(monkeypatch):
    """IN-8 end-to-end: a conversation with a non-empty linked_objects
    merges with the linked ticket's turns into a single ticket row, keyed
    by the lower of the two real ids — not two separate rows."""
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: create_calls.append(external_id) or "merged-ticket-id",
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    inserted = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages",
        lambda ticket_id, org_id, turns: inserted.update(ticket_id=ticket_id, turns=turns),
    )
    stats_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "set_ticket_provider_stats",
        lambda *a, **k: stats_calls.append((a, k)),
    )

    convo = {
        **REAL_SHAPED_CONVERSATION,
        "linked_objects": {"data": [{"type": "ticket", "id": "9"}]},  # lower id than conv-123
    }
    monkeypatch.setattr(intercom_ingest.intercom_client, "get_conversation", lambda *a, **k: convo)
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_ticket", lambda *a, **k: REAL_SHAPED_TICKET,
    )

    result = intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert result == "merged-ticket-id"
    assert create_calls == ["group:9"]
    # 3 conversation turns (source + 2 comments) + 2 ticket turns (description + 1 comment)
    assert len(inserted["turns"]) == 5
    assert [t["seq"] for t in inserted["turns"]] == [0, 1, 2, 3, 4]
    # Ambiguous which member's statistics "the" case should carry — skipped for a group.
    assert stats_calls == []


def test_ingest_intercom_conversation_dedupes_against_an_existing_group(monkeypatch):
    """A conversation already known to belong to an ingested group (via
    the group's own key) is deduped without creating a second row —
    even though its own kind-prefixed key was never used."""
    lookups = []

    def _find(org_id, *, source, external_id):
        lookups.append(external_id)
        return "existing-group-ticket-id" if external_id == "group:9" else None

    monkeypatch.setattr(intercom_ingest.ticket_ingest, "find_ticket_by_external_id", _find)
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    convo = {
        **REAL_SHAPED_CONVERSATION,
        "linked_objects": {"data": [{"type": "ticket", "id": "9"}]},
    }
    monkeypatch.setattr(intercom_ingest.intercom_client, "get_conversation", lambda *a, **k: convo)

    def _boom_if_called(*a, **k):
        raise AssertionError("must not fetch a linked member once the group is already known")

    monkeypatch.setattr(intercom_ingest.intercom_client, "get_ticket", _boom_if_called)
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda *a, **k: create_calls.append(1) or "new-ticket-id",
    )

    result = intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert result == "existing-group-ticket-id"
    assert lookups == ["conversation:conv-123", "group:9"]
    assert create_calls == []


def test_ingest_intercom_conversation_standalone_when_linked_objects_empty(monkeypatch):
    """No linked_objects — unchanged pre-IN-8 behavior, keyed by the
    conversation's own id, not a group key."""
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: create_calls.append(external_id) or "t1",
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_provider_stats", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation",
        lambda *a, **k: REAL_SHAPED_CONVERSATION,
    )
    intercom_ingest.ingest_intercom_conversation("org-1", "conv-123")
    assert create_calls == ["conversation:conv-123"]


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
    assert turns[0]["internal_contribution"] is False


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


def test_ingest_intercom_ticket_merges_a_linked_conversation_too(monkeypatch):
    """IN-8 symmetry: grouping works the same starting from a ticket as
    from a conversation, since both entry points share
    _ingest_intercom_object."""
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: create_calls.append(external_id) or "merged-id",
    )
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    inserted = {}
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages",
        lambda ticket_id, org_id, turns: inserted.update(turns=turns),
    )
    ticket = {
        **REAL_SHAPED_TICKET,
        "linked_objects": {"data": [{"type": "conversation", "id": "8"}]},  # lower than ticket-99
    }
    monkeypatch.setattr(intercom_ingest.intercom_client, "get_ticket", lambda *a, **k: ticket)
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_conversation",
        lambda *a, **k: REAL_SHAPED_CONVERSATION,
    )
    result = intercom_ingest.ingest_intercom_ticket("org-1", "ticket-99")
    assert result == "merged-id"
    assert create_calls == ["group:8"]
    assert len(inserted["turns"]) == 5  # 2 ticket turns + 3 conversation turns


def test_ingest_intercom_ticket_never_touches_provider_stats(monkeypatch):
    """Tickets don't carry a `statistics` field at all (confirmed against
    Intercom's own reference — Conversation-only) — the ticket ingest path
    must not reference set_ticket_provider_stats, even to no-op it."""
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
    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_status", lambda *a, **k: None)
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "insert_ticket_messages", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.intercom_client, "get_ticket", lambda token, tid: REAL_SHAPED_TICKET,
    )

    def _boom(*a, **k):
        raise AssertionError("ingest_intercom_ticket must not call set_ticket_provider_stats")

    monkeypatch.setattr(intercom_ingest.ticket_ingest, "set_ticket_provider_stats", _boom)
    intercom_ingest.ingest_intercom_ticket("org-1", "ticket-1")


def test_ingest_intercom_ticket_creates_no_row_when_seed_fetch_fails(monkeypatch):
    """IN-8: same ordering change as the conversation side — the seed
    fetch happens before create_ticket, so a failure there creates no
    row (still logged at the api.py call site regardless)."""
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "find_ticket_by_external_id", lambda *a, **k: None,
    )
    monkeypatch.setattr(
        intercom_ingest.org_vault, "load_credential",
        lambda *a, **k: {"access_token": "tok"},
    )
    create_calls = []
    monkeypatch.setattr(
        intercom_ingest.ticket_ingest, "create_ticket",
        lambda org_id, *, source, external_id: create_calls.append(external_id) or "new-ticket-id",
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
    assert create_calls == []
    assert statuses == []
