"""Placeholder org self-heal for unbound JustCall / QA / usage fallbacks."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace

from backend.auth import ensure_placeholder_org
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT
from backend.pyai_usage import record_http_response
from backend.transcribe import save_transcript


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


def test_ensure_placeholder_org_is_idempotent_seed_only():
    conn = _RecordingConn()
    ensure_placeholder_org(conn)
    text = " ".join(conn.sql).upper()
    assert "INSERT INTO ORGS" in text
    assert "ON CONFLICT (ID) DO NOTHING" in text
    insert = next(
        p for s, p in zip(conn.sql, conn.params) if "INSERT INTO ORGS" in s.upper()
    )
    assert insert == (DEFAULT_ORG_ID, "default")


def test_ensure_placeholder_org_not_used_in_membership_bootstrap():
    auth = (ROOT / "backend" / "auth.py").read_text(encoding="utf-8")
    start = auth.index("def ensure_membership")
    end = auth.index("def ensure_placeholder_org")
    assert "ensure_placeholder_org(" not in auth[start:end]


def test_save_transcript_heals_placeholder_org_when_missing(monkeypatch):
    healed: list = []
    monkeypatch.setattr("backend.auth.ensure_placeholder_org", lambda conn: healed.append(True))
    conn = _RecordingConn()
    save_transcript(
        conn, "file-sha256:abc", "job-1", {"text": "", "segments": []},
        org_id=DEFAULT_ORG_ID,
    )
    assert healed == [True]


def test_save_transcript_does_not_heal_for_other_orgs(monkeypatch):
    healed: list = []
    monkeypatch.setattr("backend.auth.ensure_placeholder_org", lambda conn: healed.append(True))
    conn = _RecordingConn()
    save_transcript(
        conn, "file-sha256:abc", "job-1", {"text": "", "segments": []},
        org_id=str(uuid.uuid4()),
    )
    assert healed == []


def test_usage_record_heals_when_unbound(monkeypatch):
    healed: list = []

    @contextmanager
    def _cm():
        yield _RecordingConn()

    monkeypatch.setattr("backend.pyai_usage.db.connection", _cm)
    monkeypatch.setattr("backend.auth.ensure_placeholder_org", lambda conn: healed.append(True))
    resp = SimpleNamespace(status_code=200, headers={}, request=None)
    record_http_response(resp, provider="pyai", method="GET", url="https://api.pyai.com/v1/x")
    assert healed == [True]


def test_fallback_call_sites_invoke_placeholder_heal():
    transcribe = (ROOT / "backend" / "transcribe.py").read_text(encoding="utf-8")
    qa = (ROOT / "backend" / "qa_engine.py").read_text(encoding="utf-8")
    usage = (ROOT / "backend" / "pyai_usage.py").read_text(encoding="utf-8")
    auth = (ROOT / "backend" / "auth.py").read_text(encoding="utf-8")
    assert "ensure_placeholder_org" in transcribe
    assert "ensure_placeholder_org" in qa
    assert "ensure_placeholder_org" in usage
    assert "bypass_rls=True" not in transcribe
    assert "bypass_rls=True" not in qa
    assert "bypass_rls=True" not in usage
    assert "bypass_rls=True" not in auth
