"""AC-17: per-call delete (always on) and bulk "Clear cache" (flag-gated).

Both soft-delete via calls.deleted_at/deleted_by instead of hard DELETE —
transcripts (segments/raw_json) and audit scores (audits.findings) are kept;
only the playback recording is removed. Customer-facing reads exclude
soft-deleted calls; the admin console does the opposite (AC-13/14/16 in
tests/test_admin_console.py cover that side).
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from tests.conftest import authorize

ORG_A = DEFAULT_ORG_ID


class _Cursor:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Answers the exact queries clear_cache()/delete_call() issue."""

    def __init__(self, calls):
        # calls: {call_id: {"org_id": ..., "deleted_at": None, "deleted_by": None}}
        self.calls = calls
        self.queries: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.queries.append((norm, params))
        if norm.startswith("SELECT ID FROM CALLS WHERE ORG_ID"):
            oid = params[0]
            ids = [
                cid for cid, c in self.calls.items()
                if c["org_id"] == oid and c["deleted_at"] is None
            ]
            return _Cursor([{"id": cid} for cid in ids])
        if "UPDATE CALLS SET DELETED_AT = NOW(), DELETED_BY = %S" in norm and "RETURNING ID" in norm:
            call_id, oid = params[1], params[2]
            actor = params[0]
            c = self.calls.get(call_id)
            if not c or c["org_id"] != oid or c["deleted_at"] is not None:
                return _Cursor([])
            c["deleted_at"] = "now"
            c["deleted_by"] = actor
            return _Cursor([{"id": call_id}])
        if "UPDATE CALLS SET DELETED_AT = NOW(), DELETED_BY = %S" in norm:
            actor, oid = params[0], params[1]
            n = 0
            for c in self.calls.values():
                if c["org_id"] == oid and c["deleted_at"] is None:
                    c["deleted_at"] = "now"
                    c["deleted_by"] = actor
                    n += 1
            return _Cursor([], rowcount=n)
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_conn_ctx(conn):
    yield conn


def _patch_common(monkeypatch, conn, *, audio_removed=None):
    monkeypatch.setattr("backend.api._conn", lambda: conn)
    calls = []

    def _clear(org_id, call_ids=None):
        calls.append((org_id, call_ids))
        return audio_removed if audio_removed is not None else len(call_ids or [])

    monkeypatch.setattr("backend.api._clear_playback_audio", _clear)
    return calls


# ---------- clear_cache: gated, soft-delete only ----------


def test_clear_cache_403_when_flag_off(monkeypatch):
    monkeypatch.setattr("backend.api.org_features.features_for_org", lambda oid: {})
    conn = _FakeConn({1: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None}})
    _patch_common(monkeypatch, conn)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/cache/clear")
    assert r.status_code == 403
    assert all("UPDATE" not in q[0] for q in conn.queries)


def test_clear_cache_soft_deletes_when_flag_on(monkeypatch):
    monkeypatch.setattr(
        "backend.api.org_features.features_for_org",
        lambda oid: {"enable_bulk_call_clear": True},
    )
    calls = {
        1: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None},
        2: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None},
        3: {"org_id": "other-org", "deleted_at": None, "deleted_by": None},
    }
    conn = _FakeConn(calls)
    seen_audio = _patch_common(monkeypatch, conn)
    from backend.api import app

    client = TestClient(app)
    uid = authorize(client, monkeypatch, org_id=ORG_A)
    r = client.post("/api/cache/clear")
    assert r.status_code == 200
    body = r.json()
    assert body["deleted"]["calls"] == 2
    assert "segments" not in body["deleted"]
    assert "audits" not in body["deleted"]
    assert calls[1]["deleted_at"] == "now"
    assert calls[1]["deleted_by"] == uid
    assert calls[2]["deleted_at"] == "now"
    assert calls[3]["deleted_at"] is None  # other org untouched
    assert seen_audio == [(ORG_A, [1, 2])]


def test_clear_cache_no_longer_hard_deletes_segments_or_audits():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index('@app.post("/api/cache/clear")')
    end = src.index("@app.delete(\"/api/calls/{call_id}\")", start)
    region = src[start:end].upper()
    assert "DELETE FROM AUDITS" not in region
    assert "DELETE FROM SEGMENTS" not in region
    assert "DELETE FROM CALLS" not in region
    assert "UPDATE CALLS SET DELETED_AT" in region
    assert "ENABLE_BULK_CALL_CLEAR" in region


# ---------- delete_call: always on, single call ----------


def test_delete_call_soft_deletes_one_call_and_its_audio_only(monkeypatch):
    calls = {
        1: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None},
        2: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None},
    }
    conn = _FakeConn(calls)
    seen_audio = _patch_common(monkeypatch, conn)
    from backend.api import app

    client = TestClient(app)
    uid = authorize(client, monkeypatch, org_id=ORG_A)
    r = client.delete("/api/calls/1")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "call_id": 1}
    assert calls[1]["deleted_at"] == "now"
    assert calls[1]["deleted_by"] == uid
    assert calls[2]["deleted_at"] is None
    assert seen_audio == [(ORG_A, [1])]


def test_delete_call_does_not_require_any_feature_flag(monkeypatch):
    """Per-call delete is user-facing and always available (AC-17) — unlike
    Clear cache, it must not consult org_features at all."""
    calls = {1: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None}}
    conn = _FakeConn(calls)
    _patch_common(monkeypatch, conn)

    def boom(*a, **k):
        raise AssertionError("delete_call must not check org_features")

    monkeypatch.setattr("backend.api.org_features.features_for_org", boom)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.delete("/api/calls/1")
    assert r.status_code == 200


def test_delete_call_404_for_unknown_other_org_or_already_deleted(monkeypatch):
    calls = {
        1: {"org_id": ORG_A, "deleted_at": None, "deleted_by": None},
        2: {"org_id": "other-org", "deleted_at": None, "deleted_by": None},
        3: {"org_id": ORG_A, "deleted_at": "now", "deleted_by": "someone"},
    }
    conn = _FakeConn(calls)
    _patch_common(monkeypatch, conn)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    assert client.delete("/api/calls/999").status_code == 404
    assert client.delete("/api/calls/2").status_code == 404
    assert client.delete("/api/calls/3").status_code == 404


# ---------- customer-facing reads exclude soft-deleted calls ----------


def test_list_calls_query_filters_deleted():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index('@app.get("/api/calls")')
    end = src.index("def list_calls", start)
    end = src.index("\n\n", src.index("ORDER BY c.id DESC", start))
    region = src[start:end]
    assert "c.deleted_at IS NULL" in region


def test_get_audit_404s_for_deleted_call(monkeypatch):
    conn = _FakeConn({7: {"org_id": ORG_A, "deleted_at": "now", "deleted_by": "x"}})
    monkeypatch.setattr("backend.api._conn", lambda: conn)

    def _no_such_call(sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("SELECT 1 FROM CALLS"):
            return _Cursor([])
        raise AssertionError(f"unexpected query: {norm}")

    conn.execute = _no_such_call
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/calls/7/audit")
    assert r.status_code == 404


def test_transcribe_lookups_exclude_soft_deleted():
    src = (ROOT / "backend" / "transcribe.py").read_text(encoding="utf-8")

    def region(fn_name):
        start = src.index(f"def {fn_name}")
        end = src.index("\ndef ", start + 1)
        return src[start:end]

    assert "deleted_at IS NULL" in region("find_existing_call")
    assert "deleted_at IS NULL" in region("find_existing_external")
    assert "deleted_at IS NULL" in region("get_call")


def test_qa_engine_load_call_excludes_soft_deleted():
    src = (ROOT / "backend" / "qa_engine.py").read_text(encoding="utf-8")
    start = src.index("def load_call")
    end = src.index("\ndef ", start + 1)
    region = src[start:end]
    assert region.count("deleted_at IS NULL") >= 2


# ---------- migration shape ----------


def test_nineteenth_revision_adds_soft_delete_columns():
    rev = ROOT / "alembic" / "versions" / "0019_call_soft_delete.py"
    assert rev.is_file()
    raw = rev.read_text(encoding="utf-8")
    sql = raw.upper()
    assert "ADD COLUMN DELETED_AT TIMESTAMPTZ" in sql
    assert "ADD COLUMN DELETED_BY UUID REFERENCES ORG_MEMBERS (USER_ID)" in sql
    assert "CREATE INDEX IDX_CALLS_ORG_DELETED ON CALLS (ORG_ID, DELETED_AT)" in sql
    assert "0018_password_reset_events" in raw
    assert "bypass_rls" not in raw


def test_bulk_clear_flag_defaults_off():
    from backend.org_features import DEFAULT_OFF_KEYS, FEATURE_KEYS, default_features

    assert "enable_bulk_call_clear" in FEATURE_KEYS
    assert "enable_bulk_call_clear" in DEFAULT_OFF_KEYS
    assert default_features()["enable_bulk_call_clear"] is False
