"""Score persistence: once a call has a completed audit, re-scoring it
(?refresh=true, or a retranscribe) is blocked unless the org's
enable_call_rescoring flag is on — Claude isn't perfectly deterministic,
so a re-run could hand back a different score for the same call. Off by
default. A never-before-audited call is always allowed to score, regardless
of the flag."""

from __future__ import annotations

from fastapi import HTTPException
import pytest
from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID

STORED_ROW = {
    "findings": {"score": 77, "grade": "B"},
    "engine_version": "engine-hash",
    "rubric_id": "some-rubric-id",
    "rubric_version": 1,
}


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _patch_common(monkeypatch, *, flag_on: bool, prior_row):
    from backend import api

    monkeypatch.setattr(
        api.audit_store, "fetch_active_rubric",
        lambda c, *, org_id: ("rid", 1, {"name": "R"}),
    )
    monkeypatch.setattr(
        api.audit_store, "fetch_latest", lambda c, *, call_id, org_id: prior_row,
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())
    monkeypatch.setattr(api, "_rubric_hash", lambda: "engine-hash")
    monkeypatch.setattr(
        api.org_features, "features_for_org",
        lambda org_id: {"enable_call_rescoring": flag_on},
    )


# ---------- _load_or_compute_audit ----------


def test_first_time_audit_always_allowed_regardless_of_flag(monkeypatch):
    from backend import api

    _patch_common(monkeypatch, flag_on=False, prior_row=None)
    monkeypatch.setattr(
        api, "analyze_call", lambda call_id, org_id, rubric: {"score": 50, "grade": "C"},
    )
    monkeypatch.setattr(api.audit_store, "upsert_audit", lambda c, **kw: "audit-id")

    audit, _rh, _rid, _rv = api._load_or_compute_audit(7, ORG_A, refresh=True)
    assert audit["score"] == 50


def test_refresh_blocked_when_flag_off_and_already_audited(monkeypatch):
    from backend import api

    _patch_common(monkeypatch, flag_on=False, prior_row=STORED_ROW)

    with pytest.raises(HTTPException) as exc:
        api._load_or_compute_audit(7, ORG_A, refresh=True)
    assert exc.value.status_code == 403


def test_refresh_allowed_when_flag_on(monkeypatch):
    from backend import api

    _patch_common(monkeypatch, flag_on=True, prior_row=STORED_ROW)
    monkeypatch.setattr(
        api, "analyze_call", lambda call_id, org_id, rubric: {"score": 90, "grade": "A"},
    )
    monkeypatch.setattr(api.audit_store, "upsert_audit", lambda c, **kw: "audit-id")

    audit, _rh, _rid, _rv = api._load_or_compute_audit(7, ORG_A, refresh=True)
    assert audit["score"] == 90


def test_non_refresh_cache_hit_unaffected_by_flag_being_off(monkeypatch):
    from backend import api

    _patch_common(monkeypatch, flag_on=False, prior_row=STORED_ROW)

    audit, _rh, _rid, _rv = api._load_or_compute_audit(7, ORG_A, refresh=False)
    assert audit["score"] == 77


# ---------- GET /api/calls/{id}/audit ----------


def test_get_audit_refresh_query_param_blocked_when_flag_off(monkeypatch):
    from backend import api
    from backend.api import app

    _patch_common(monkeypatch, flag_on=False, prior_row=STORED_ROW)
    monkeypatch.setattr(api, "_conn", lambda: _ExistsThenAuditConn())

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/calls/7/audit", params={"refresh": "true"})
    assert r.status_code == 403


class _Cursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _ExistsThenAuditConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("SELECT 1 FROM CALLS"):
            return _Cursor([{"1": 1}])
        raise AssertionError(f"unexpected query: {norm}")


# ---------- POST /api/calls/{id}/retranscribe ----------


def test_retranscribe_blocked_before_any_transcribe_work_when_flag_off(monkeypatch):
    """Rejected up front, before replacing the transcript — never left with
    a new transcript paired to a stale, un-refreshable score."""
    from backend import api
    from backend.api import app

    monkeypatch.setattr(
        api.transcribe, "get_call", lambda c, call_id, *, org_id: {"id": call_id},
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())
    monkeypatch.setattr(
        api.audit_store, "fetch_latest", lambda c, *, call_id, org_id: STORED_ROW,
    )
    monkeypatch.setattr(
        api.org_features, "features_for_org", lambda org_id: {"enable_call_rescoring": False},
    )

    def _boom(*a, **k):
        raise AssertionError("must not start transcription once already-audited and blocked")

    monkeypatch.setattr(api.audio_store, "download_to_temp", _boom)
    monkeypatch.setattr(api.transcribe, "transcribe_audio", _boom)
    monkeypatch.setattr(api.transcribe, "replace_transcript", _boom)

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/calls/7/retranscribe")
    assert r.status_code == 403


def test_retranscribe_allowed_when_flag_on(monkeypatch):
    from contextlib import contextmanager

    from backend import api
    from backend.api import app

    monkeypatch.setattr(
        api.transcribe, "get_call", lambda c, call_id, *, org_id: {"id": call_id},
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())
    monkeypatch.setattr(
        api.audit_store, "fetch_latest", lambda c, *, call_id, org_id: STORED_ROW,
    )
    monkeypatch.setattr(
        api.org_features, "features_for_org", lambda org_id: {"enable_call_rescoring": True},
    )

    @contextmanager
    def _fake_download(org_id, call_id):
        yield "/tmp/fake.wav"

    monkeypatch.setattr(api.audio_store, "download_to_temp", _fake_download)
    monkeypatch.setattr(api.os.path, "getsize", lambda path: 1234)
    monkeypatch.setattr(api.transcribe, "new_pyai_call_id", lambda: "pyai-1")
    monkeypatch.setattr(
        api.transcribe, "transcribe_audio",
        lambda path, hear_tmp, *, call_id, org_id: ("job-1", {"segments": []}, "diarize"),
    )
    monkeypatch.setattr(api.transcribe, "replace_transcript", lambda conn, *a, **k: None)
    monkeypatch.setattr(
        api, "_load_or_compute_audit",
        lambda call_id, org_id, refresh=False, requested_by=None: (
            {"score": 95}, "rh", "rid", 1,
        ),
    )

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/calls/7/retranscribe")
    assert r.status_code == 200
    assert r.json()["score"] == 95
