"""AC-43/AC-44: product_events (append-only usage telemetry)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

import pytest

from backend.paths import ROOT

ORG_A = str(uuid.uuid4())
USER_A = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, *, boom: bool = False):
        self.inserted: list[tuple] = []
        self.boom = boom

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        if norm.startswith("INSERT INTO PRODUCT_EVENTS"):
            if self.boom:
                raise RuntimeError("db is down")
            self.inserted.append(params)
            return _Result([])
        raise AssertionError(f"unexpected query: {norm}")


@contextmanager
def _fake_db(monkeypatch, conn: _FakeConn):
    @contextmanager
    def _cm(*_a, **_k):
        yield conn

    monkeypatch.setattr("backend.product_events.db.connection", _cm)
    monkeypatch.setattr("backend.product_events.org_scope", lambda oid: _cm())
    yield conn


def test_track_event_inserts_a_row_for_an_allowed_event(monkeypatch):
    from backend import product_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        product_events.track_event(ORG_A, USER_A, "call_uploaded", {"source": "manual"})
    assert len(conn.inserted) == 1
    org_id, user_id, event_name, properties = conn.inserted[0]
    assert org_id == ORG_A
    assert user_id == USER_A
    assert event_name == "call_uploaded"
    assert properties.obj == {"source": "manual"}


def test_track_event_allows_a_null_user_id(monkeypatch):
    from backend import product_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        product_events.track_event(ORG_A, None, "session_started", {})
    assert conn.inserted[0][1] is None


def test_track_event_defaults_properties_to_empty_dict(monkeypatch):
    from backend import product_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        product_events.track_event(ORG_A, USER_A, "session_started")
    assert conn.inserted[0][3].obj == {}


def test_track_event_never_raises_on_an_unknown_event_name(monkeypatch):
    """The allowlist is deliberately closed (PRD's "dumping ground" risk) —
    an unknown name is skipped and logged, not inserted, and never crashes
    the caller."""
    from backend import product_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        product_events.track_event(ORG_A, USER_A, "literally_anything")
    assert conn.inserted == []


def test_track_event_never_raises_on_an_invalid_org_id(monkeypatch):
    from backend import product_events

    conn = _FakeConn()
    with _fake_db(monkeypatch, conn):
        product_events.track_event("not-a-uuid", USER_A, "session_started")
    assert conn.inserted == []


def test_track_event_never_raises_when_the_db_write_fails(monkeypatch):
    """Best-effort by design — a telemetry bug or transient DB error must
    never break the real feature it's attached to."""
    from backend import product_events

    conn = _FakeConn(boom=True)
    with _fake_db(monkeypatch, conn):
        product_events.track_event(ORG_A, USER_A, "session_started")  # must not raise


def test_allowed_events_covers_every_event_from_the_prd():
    from backend import product_events

    expected = {
        "call_uploaded", "ticket_uploaded", "rubric_builder_opened", "rubric_saved",
        "churn_risk_viewed", "stakeholder_email_drafted", "flag_created", "flag_solved",
        "feedback_requested", "session_started", "upload_failed", "batch_partial_failure",
    }
    assert expected.issubset(product_events.ALLOWED_EVENTS)


def test_backend_wired_and_frontend_only_events_partition_the_allowlist():
    from backend import product_events

    assert product_events.BACKEND_WIRED_EVENTS | product_events.FRONTEND_ONLY_EVENTS == product_events.ALLOWED_EVENTS
    assert product_events.BACKEND_WIRED_EVENTS & product_events.FRONTEND_ONLY_EVENTS == set()


def test_module_never_bypasses_rls():
    src = (ROOT / "backend" / "product_events.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src
