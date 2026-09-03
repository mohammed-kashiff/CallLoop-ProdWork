"""CR-12: every audit write stamps the rubric_id/rubric_version that
actually produced it, instead of silently defaulting to the legacy
constant regardless of which rubric was active. Also: a previously-scored
call's cached result is never invalidated just because the org's active
rubric changed since (weight edits apply going forward only)."""

from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID


class _NullConn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fresh_compute_stamps_the_actually_resolved_rubric_id_and_version(monkeypatch):
    from backend import api

    monkeypatch.setattr(
        api.audit_store, "fetch_active_rubric",
        lambda c, *, org_id: ("custom-rubric-id", 2, {"name": "Custom"}),
    )
    monkeypatch.setattr(
        api.audit_store, "fetch_latest", lambda c, *, call_id, org_id: None,  # cache miss
    )
    monkeypatch.setattr(
        api, "analyze_call", lambda call_id, org_id, rubric: {"score": 91, "grade": "A"},
    )
    upserts = []
    monkeypatch.setattr(
        api.audit_store, "upsert_audit",
        lambda c, **kw: upserts.append(kw) or "audit-id",
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())
    monkeypatch.setattr(api, "_rubric_hash", lambda: "engine-hash")

    audit, rh, rubric_id, rubric_version = api._load_or_compute_audit(7, ORG_A)

    assert rubric_id == "custom-rubric-id"
    assert rubric_version == 2
    assert audit["score"] == 91
    assert len(upserts) == 1
    assert upserts[0]["rubric_id"] == "custom-rubric-id"
    assert upserts[0]["rubric_version"] == 2


def test_cache_hit_returns_the_stored_version_not_the_current_active_one(monkeypatch):
    """After an org's active rubric changes, re-reading an already-scored
    call without refresh must show the version that actually scored it —
    not silently swap to whatever is active now."""
    from backend import api

    monkeypatch.setattr(
        api.audit_store, "fetch_active_rubric",
        lambda c, *, org_id: ("new-active-id", 5, {"name": "New"}),
    )
    stored_row = {
        "findings": {"score": 42, "grade": "B"},
        "engine_version": "engine-hash",
        "rubric_id": "old-id-that-scored-this-call",
        "rubric_version": 1,
    }
    monkeypatch.setattr(
        api.audit_store, "fetch_latest", lambda c, *, call_id, org_id: stored_row,
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())
    monkeypatch.setattr(api, "_rubric_hash", lambda: "engine-hash")

    audit, rh, rubric_id, rubric_version = api._load_or_compute_audit(7, ORG_A, refresh=False)

    assert audit["score"] == 42
    assert rubric_id == "old-id-that-scored-this-call"
    assert rubric_version == 1


def test_save_audit_requires_and_forwards_rubric_id_and_version(monkeypatch):
    from backend import api

    upserts = []
    monkeypatch.setattr(
        api.audit_store, "upsert_audit", lambda c, **kw: upserts.append(kw),
    )
    monkeypatch.setattr(api, "_conn", lambda: _NullConn())

    api._save_audit(
        7, {"score": 1}, "hash", ORG_A, rubric_id="old-id", rubric_version=3,
    )
    assert upserts[0]["rubric_id"] == "old-id"
    assert upserts[0]["rubric_version"] == 3


def test_save_audit_signature_requires_rubric_id_and_version():
    import inspect

    from backend.api import _save_audit

    params = inspect.signature(_save_audit).parameters
    assert params["rubric_id"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["rubric_id"].default is inspect.Parameter.empty
    assert params["rubric_version"].kind == inspect.Parameter.KEYWORD_ONLY
    assert params["rubric_version"].default is inspect.Parameter.empty


class _Cursor:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FlagFakeConn:
    """Answers flag_call_for_review's two queries: the existing audit row
    (with its ORIGINAL rubric_id/version) and the resulting upsert."""

    def __init__(self, row):
        self.row = row
        self.upserts: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("SELECT *") and "FROM AUDITS" in norm:
            return _Cursor([self.row])
        if norm.startswith("INSERT INTO AUDITS"):
            self.upserts.append(params)
            return _Cursor([])
        if "FROM RUBRICS WHERE ID" in norm:
            return _Cursor([{"id": params[0]}])  # seed_legacy_rubric no-op guard
        raise AssertionError(f"unexpected query: {norm}")


def test_flag_route_preserves_the_audits_original_rubric_id_and_version(monkeypatch):
    """The org's active rubric may have moved on since this call was
    scored — flagging it must not re-stamp it as if today's rubric had
    produced it."""
    from backend.api import app

    stored_row = {
        "id": "audit-1", "org_id": ORG_A, "call_id": 7,
        "rubric_id": "historical-rubric-id", "rubric_version": 1,
        "engine_version": "engine-hash",
        "findings": {"score": 55, "grade": "C", "flagged": False},
    }
    conn = _FlagFakeConn(stored_row)
    monkeypatch.setattr("backend.api._conn", lambda: conn)
    monkeypatch.setattr("backend.api._rubric_hash", lambda: "engine-hash")

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/calls/7/flag")
    assert r.status_code == 200
    assert len(conn.upserts) == 1
    # INSERT INTO audits (id, org_id, call_id, rubric_id, rubric_version, ...)
    inserted_rubric_id, inserted_rubric_version = conn.upserts[0][3], conn.upserts[0][4]
    assert inserted_rubric_id == "historical-rubric-id"
    assert inserted_rubric_version == 1


def test_no_call_site_in_api_defaults_rubric_id_or_version_silently():
    """Static guard: every _save_audit call in api.py must pass rubric_id=
    and rubric_version= explicitly — the whole point of this ticket is that
    nothing may fall back to upsert_audit's defaults implicitly again."""
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")

    def _balanced_call_args(text: str, open_paren_idx: int) -> str:
        depth = 0
        for i in range(open_paren_idx, len(text)):
            if text[i] == "(":
                depth += 1
            elif text[i] == ")":
                depth -= 1
                if depth == 0:
                    return text[open_paren_idx + 1:i]
        raise AssertionError("unbalanced parens")

    calls = []
    idx = 0
    while True:
        idx = src.find("_save_audit(", idx)
        if idx == -1:
            break
        preceding = src[max(0, idx - 4):idx]
        if not preceding.endswith("def "):
            calls.append(_balanced_call_args(src, idx + len("_save_audit") - 1))
        idx += 1

    assert calls, "expected at least one _save_audit call site"
    for call_args in calls:
        assert "rubric_id=" in call_args, call_args
        assert "rubric_version=" in call_args, call_args
