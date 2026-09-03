"""CR-11: org-aware rubric loading (FR1) — analyze_call reads the org's
active rubrics row from Postgres, falling back to rubric.json only when the
org has no active row. No proactive seeding (CR-13 creates rows).

audit_store.fetch_active_rubric() returns (rubric_id, version, definition)
together in one query, resolved once (CR-12 needs the id/version alongside
the definition to correctly stamp the audit that gets scored with it)."""

from __future__ import annotations

import uuid

from backend.org_ids import DEFAULT_RUBRIC_ID
from backend.paths import ROOT

ORG_A = "00000000-0000-4000-8000-0000000000aa"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows_by_org):
        self.rows_by_org = rows_by_org  # {org_id: (id, version, definition) or None}
        self.queries: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.queries.append((norm, params))
        assert "FROM RUBRICS" in norm
        assert "ORG_ID = %S" in norm
        assert "IS_ACTIVE" in norm
        org_id = params[0]
        row = self.rows_by_org.get(org_id)
        if row is None:
            return _Result([])
        rid, version, definition = row
        return _Result([{"id": rid, "version": version, "definition": definition}])


def test_fetch_active_rubric_falls_back_to_legacy_identity_when_no_active_row():
    from backend.audit_store import LEGACY_RUBRIC_VERSION, fetch_active_rubric, load_v8_definition

    conn = _FakeConn({})
    rubric_id, version, definition = fetch_active_rubric(conn, org_id=ORG_A)
    assert rubric_id == DEFAULT_RUBRIC_ID
    assert version == LEGACY_RUBRIC_VERSION
    assert definition == load_v8_definition()
    assert conn.queries[0][1] == (ORG_A,)


def test_fetch_active_rubric_returns_the_active_row_not_the_file():
    from backend.audit_store import fetch_active_rubric, load_v8_definition

    custom = load_v8_definition()
    custom["technical_skills"]["dimensions"][0]["weight"] = 10
    row_id = str(uuid.uuid4())
    conn = _FakeConn({ORG_A: (row_id, 2, custom)})
    rubric_id, version, definition = fetch_active_rubric(conn, org_id=ORG_A)
    assert rubric_id == row_id
    assert version == 2
    assert definition == custom
    assert definition != load_v8_definition()


def test_fetch_active_rubric_decodes_jsonb_as_string_too():
    """decode_findings already handles TEXT-era JSON strings; reuse that
    defensiveness here rather than assuming psycopg always hands back a dict."""
    import json

    from backend.audit_store import fetch_active_rubric, load_v8_definition

    custom = load_v8_definition()
    row_id = str(uuid.uuid4())

    class _StringRowConn(_FakeConn):
        def execute(self, sql, params=None):
            self.queries.append((str(sql), params))
            return _Result([{"id": row_id, "version": 3, "definition": json.dumps(custom)}])

    rubric_id, version, definition = fetch_active_rubric(_StringRowConn({}), org_id=ORG_A)
    assert rubric_id == row_id
    assert version == 3
    assert definition == custom


def test_analyze_call_takes_rubric_as_a_required_parameter():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def analyze_call(")
    end = src.index("\ndef ", start + 1)
    region = src[start:end]
    assert "open(qa.RUBRIC_PATH)" not in region
    assert "= audit_store.fetch_active_rubric(" not in region
    assert "*, rubric: dict" in region


def test_live_org_with_no_row_falls_back_and_org_with_active_row_gets_it():
    """Live Postgres: real RLS-scoped read, not a mocked shortcut (CR-11's
    stated acceptance criterion)."""
    import json

    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row
    from psycopg.types.json import Json

    from backend.audit_store import LEGACY_RUBRIC_VERSION, fetch_active_rubric, load_v8_definition
    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    org_untouched = str(uuid.uuid4())
    org_custom = str(uuid.uuid4())
    custom_def = load_v8_definition()
    custom_def["technical_skills"]["dimensions"][0]["weight"] = 5
    custom_def["soft_skills"]["dimensions"][0]["weight"] = 55
    custom_rubric_id = str(uuid.uuid4())

    conn = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    conn.autocommit = False
    try:
        conn.execute(
            "INSERT INTO orgs (id, name) VALUES (%s, %s), (%s, %s)",
            (org_untouched, "cr11-untouched", org_custom, "cr11-custom"),
        )
        conn.execute(
            """
            INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            """,
            (custom_rubric_id, org_custom, "Custom (CR-11 test)", 2, Json(custom_def)),
        )

        untouched_id, untouched_version, untouched_def = fetch_active_rubric(
            conn, org_id=org_untouched,
        )
        assert untouched_id == DEFAULT_RUBRIC_ID
        assert untouched_version == LEGACY_RUBRIC_VERSION
        assert untouched_def == load_v8_definition()

        got_id, got_version, got_def = fetch_active_rubric(conn, org_id=org_custom)
        assert got_id == custom_rubric_id
        assert got_version == 2
        assert json.dumps(got_def, sort_keys=True) == json.dumps(custom_def, sort_keys=True)
        assert got_def != load_v8_definition()
    finally:
        conn.rollback()
        conn.close()
