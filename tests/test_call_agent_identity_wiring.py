"""IN-24/25: the two places agent_user_id actually gets set on a call —
save_transcript()'s uploaded_by fallback, and _ingest_audio_file()'s
resolve-before-insert call into call_agent_identity_aliases.

The alias module's own CRUD/resolution logic is covered by
test_call_agent_identity_aliases.py; this file covers the wiring that
connects it to real ingestion, matching the shape of the existing
save_transcript tests in test_placeholder_org.py."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from backend.org_ids import DEFAULT_ORG_ID
from backend.transcribe import save_transcript


@contextmanager
def _fake_db_connection(*_a, **_k):
    yield object()


class _Row:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _RecordingConn:
    def __init__(self):
        self.sql: list[str] = []
        self.params: list = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))
        self.params.append(params)
        norm = self.sql[-1].upper()
        if "INSERT INTO CALLS" in norm and "RETURNING" in norm:
            return _Row({"id": 1})
        return _Row(None)

    def commit(self) -> None:
        return


def _insert_call_params(conn: _RecordingConn) -> tuple:
    for sql, params in zip(conn.sql, conn.params):
        if "INSERT INTO CALLS" in sql.upper() and "RETURNING" in sql.upper():
            return params
    raise AssertionError("no INSERT INTO calls was issued")


# ---------- save_transcript: agent_user_id / agent_identifier ----------


def test_save_transcript_uses_agent_user_id_when_given():
    """The JustCall path: an already-resolved alias is passed straight
    through, taking priority over uploaded_by (a manager who happened to
    trigger the sync isn't the agent on the call)."""
    conn = _RecordingConn()
    agent = str(uuid.uuid4())
    manager = str(uuid.uuid4())
    save_transcript(
        conn, "justcall:123", "job-1", {"text": "", "segments": []},
        org_id=DEFAULT_ORG_ID, uploaded_by=manager, agent_user_id=agent,
        agent_identifier="agent@justcall.example",
    )
    params = _insert_call_params(conn)
    # INSERT INTO calls (..., uploaded_by, agent_user_id, agent_identifier)
    assert params[-3] == manager
    assert params[-2] == agent
    assert params[-1] == "agent@justcall.example"


def test_save_transcript_falls_back_to_uploaded_by_when_no_agent_user_id():
    """The manual-upload path (IN-25): no alias resolution happened, so
    uploaded_by (already a real org_members.user_id) becomes the agent —
    no guessing needed, unlike calls' old heuristic name-guess."""
    conn = _RecordingConn()
    uploader = str(uuid.uuid4())
    save_transcript(
        conn, "file-sha256:abc", "job-1", {"text": "", "segments": []},
        org_id=DEFAULT_ORG_ID, uploaded_by=uploader,
    )
    params = _insert_call_params(conn)
    assert params[-3] == uploader
    assert params[-2] == uploader  # resolved_agent falls back to uploaded_by
    assert params[-1] is None  # no agent_identifier on a manual upload


def test_save_transcript_leaves_agent_user_id_none_when_neither_given():
    """A JustCall call with an unresolved agent_identifier and no
    uploaded_by: agent_user_id stays NULL, surfaced later via
    list_unresolved_identifiers() for the owner to map."""
    conn = _RecordingConn()
    save_transcript(
        conn, "justcall:456", "job-1", {"text": "", "segments": []},
        org_id=DEFAULT_ORG_ID, agent_identifier="unknown@justcall.example",
    )
    params = _insert_call_params(conn)
    assert params[-3] is None  # uploaded_by
    assert params[-2] is None  # agent_user_id
    assert params[-1] == "unknown@justcall.example"


# ---------- _ingest_audio_file: resolves agent_identifier before insert ----------


def test_ingest_audio_file_resolves_agent_identifier_via_alias(monkeypatch, tmp_path):
    """_ingest_audio_file must look up the alias BEFORE calling
    save_transcript — the resolved id, not the raw identifier, is what
    gets written to calls.agent_user_id."""
    from backend import api

    src = tmp_path / "call.mp3"
    src.write_bytes(b"not-real-audio")

    resolved = str(uuid.uuid4())
    calls_resolve = []

    monkeypatch.setattr(
        "backend.api.call_agent_identity_aliases.resolve_agent_user_id",
        lambda org_id, source, identifier: calls_resolve.append(
            (org_id, source, identifier)
        )
        or resolved,
    )
    monkeypatch.setattr(api.db, "connection", _fake_db_connection)
    monkeypatch.setattr(api.transcribe, "identity_for", lambda p: "justcall:789")
    monkeypatch.setattr(
        api.transcribe, "find_existing_external", lambda conn, s, e, org_id: None,
    )
    monkeypatch.setattr(
        api.transcribe, "find_existing_call", lambda conn, ident, org_id: None,
    )
    monkeypatch.setattr(
        api.transcribe, "new_pyai_call_id", lambda: "pyai_1",
    )
    monkeypatch.setattr(
        api.transcribe, "transcribe_audio",
        lambda src_path, tmp, call_id, org_id: (
            "job-1", {"text": "", "segments": []}, "pyai",
        ),
    )
    captured_save = {}

    def _fake_save_transcript(conn, identity, job_id, result, **kw):
        captured_save.update(kw)
        return 42

    monkeypatch.setattr(api.transcribe, "save_transcript", _fake_save_transcript)

    call_id, deduped = api._ingest_audio_file(
        str(src), "call.mp3",
        org_id=DEFAULT_ORG_ID, source="justcall", external_id="789",
        agent_identifier="agent@justcall.example",
    )
    assert call_id == 42
    assert deduped is False
    assert calls_resolve == [(DEFAULT_ORG_ID, "justcall", "agent@justcall.example")]
    assert captured_save["agent_user_id"] == resolved
    assert captured_save["agent_identifier"] == "agent@justcall.example"


def test_ingest_audio_file_skips_resolution_without_agent_identifier(monkeypatch, tmp_path):
    """A manual upload passes no agent_identifier — the alias lookup must
    not even run (save_transcript's own uploaded_by fallback handles
    it), matching the "no changes needed" finding for /api/upload."""
    from backend import api

    src = tmp_path / "call.mp3"
    src.write_bytes(b"not-real-audio")

    called = []
    monkeypatch.setattr(
        "backend.api.call_agent_identity_aliases.resolve_agent_user_id",
        lambda *a: called.append(a),
    )
    monkeypatch.setattr(api.db, "connection", _fake_db_connection)
    monkeypatch.setattr(api.transcribe, "identity_for", lambda p: "file-sha256:xyz")
    monkeypatch.setattr(
        api.transcribe, "find_existing_external", lambda conn, s, e, org_id: None,
    )
    monkeypatch.setattr(
        api.transcribe, "find_existing_call", lambda conn, ident, org_id: None,
    )
    monkeypatch.setattr(api.transcribe, "new_pyai_call_id", lambda: "pyai_2")
    monkeypatch.setattr(
        api.transcribe, "transcribe_audio",
        lambda src_path, tmp, call_id, org_id: (
            "job-2", {"text": "", "segments": []}, "pyai",
        ),
    )
    captured_save = {}

    def _fake_save_transcript(conn, identity, job_id, result, **kw):
        captured_save.update(kw)
        return 7

    monkeypatch.setattr(api.transcribe, "save_transcript", _fake_save_transcript)

    uploader = str(uuid.uuid4())
    call_id, deduped = api._ingest_audio_file(
        str(src), "call.mp3", org_id=DEFAULT_ORG_ID, uploaded_by=uploader,
    )
    assert call_id == 7
    assert called == []
    assert captured_save["agent_user_id"] is None
    assert captured_save["uploaded_by"] == uploader


# ---------- justcall.agent_identity_for wiring into _process_justcall_call ----------


def test_justcall_agent_identity_for_reads_the_same_nested_payload_as_display_name():
    """agent_identity_for() and display_name() must both understand the
    same JustCall payload shape — a regression guard against the two
    functions drifting apart if JustCall's nested structure changes."""
    from backend import justcall

    payload = {
        "data": {
            "agent": {"name": "Kashif K", "email": "kashif@justcall.example"},
        },
    }
    assert justcall.agent_identity_for(payload) == "kashif@justcall.example"
    assert "Kashif" in justcall.display_name(payload, "789")
