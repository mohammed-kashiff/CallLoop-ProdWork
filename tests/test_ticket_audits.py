"""TA-11: ticket_audits schema — one stored scorecard per ticket, RLS,
no call-engine tables touched."""

from __future__ import annotations

from backend.paths import ROOT

REV = ROOT / "alembic" / "versions" / "0024_ticket_audits.py"
SCORING = ("qa_engine.py", "qa_v8.py", "rules_v8.py")


def _raw() -> str:
    return REV.read_text(encoding="utf-8")


def test_revision_file_and_chain():
    assert REV.is_file()
    raw = _raw()
    assert 'revision: str = "0024_ticket_audits"' in raw
    assert "0023_ticket_image_assets" in raw


def test_table_is_org_scoped_one_row_per_ticket():
    sql = _raw().upper()
    assert "CREATE TABLE TICKET_AUDITS" in sql
    assert "ORG_ID UUID NOT NULL REFERENCES ORGS" in sql
    assert "TICKET_ID UUID NOT NULL REFERENCES TICKETS" in sql
    assert "ON DELETE CASCADE" in sql
    assert "UNIQUE (TICKET_ID)" in sql
    assert "FINDINGS JSONB NOT NULL" in sql
    assert "REQUESTED_BY UUID REFERENCES ORG_MEMBERS (USER_ID)" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY TICKET_AUDITS_SELECT" in sql
    assert "CREATE POLICY TICKET_AUDITS_INSERT" in sql
    assert "CREATE POLICY TICKET_AUDITS_UPDATE" in sql
    assert "CREATE POLICY TICKET_AUDITS_DELETE" not in sql
    assert "GRANT SELECT, INSERT, UPDATE ON TICKET_AUDITS TO CALLPROOF_APP" in sql
    assert "CALLPROOF_CURRENT_ORG_ID()" in sql
    assert "bypass_rls" not in _raw()


def test_does_not_alter_calls_audits_table():
    sql = _raw().upper()
    assert "ALTER TABLE AUDITS" not in sql
    assert "CREATE TABLE AUDITS" not in sql


def test_call_scoring_engine_is_untouched():
    raw = _raw()
    for name in SCORING:
        assert name not in raw
        assert name.removesuffix(".py") not in raw
    src = (ROOT / "backend" / "ticket_audit_store.py").read_text(encoding="utf-8")
    assert "bypass_rls" not in src
    assert "from .qa_engine" not in src
    assert "from .qa_v8" not in src
    assert "from .rules_v8" not in src
    assert "from .audit_store" not in src
