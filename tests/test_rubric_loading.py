"""CR-11: org-aware rubric loading (FR1) — analyze_call reads the org's
active rubrics.definition from Postgres, falling back to rubric.json only
when the org has no active row. No proactive seeding (CR-13 creates rows)."""

from __future__ import annotations

import uuid

from backend.paths import ROOT

ORG_A = "00000000-0000-4000-8000-0000000000aa"


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, rows_by_org):
        self.rows_by_org = rows_by_org  # {org_id: definition dict or None}
        self.queries: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.queries.append((norm, params))
        assert "FROM RUBRICS" in norm
        assert "ORG_ID = %S" in norm
        assert "IS_ACTIVE" in norm
        org_id = params[0]
        definition = self.rows_by_org.get(org_id)
        if definition is None:
            return _Result([])
        return _Result([{"definition": definition}])


def test_fetch_active_definition_falls_back_to_file_when_no_active_row():
    from backend.audit_store import fetch_active_definition, load_v8_definition

    conn = _FakeConn({})
    out = fetch_active_definition(conn, org_id=ORG_A)
    assert out == load_v8_definition()
    assert conn.queries[0][1] == (ORG_A,)


def test_fetch_active_definition_returns_the_active_row_not_the_file():
    from backend.audit_store import fetch_active_definition, load_v8_definition

    custom = load_v8_definition()
    custom["technical_skills"]["dimensions"][0]["weight"] = 10
    conn = _FakeConn({ORG_A: custom})
    out = fetch_active_definition(conn, org_id=ORG_A)
    assert out == custom
    assert out != load_v8_definition()


def test_fetch_active_definition_decodes_jsonb_as_string_too():
    """decode_findings already handles TEXT-era JSON strings; reuse that
    defensiveness here rather than assuming psycopg always hands back a dict."""
    import json

    from backend.audit_store import fetch_active_definition, load_v8_definition

    custom = load_v8_definition()

    class _StringRowConn(_FakeConn):
        def execute(self, sql, params=None):
            self.queries.append((str(sql), params))
            return _Result([{"definition": json.dumps(custom)}])

    out = fetch_active_definition(_StringRowConn({}), org_id=ORG_A)
    assert out == custom


def test_analyze_call_no_longer_reads_rubric_path_directly():
    src = (ROOT / "backend" / "api.py").read_text(encoding="utf-8")
    start = src.index("def analyze_call(")
    end = src.index("\ndef ", start + 1)
    region = src[start:end]
    assert "open(qa.RUBRIC_PATH)" not in region
    assert "audit_store.fetch_active_definition(c, org_id=org_id)" in region


def test_live_org_with_no_row_falls_back_and_org_with_active_row_gets_it():
    """Live Postgres: real RLS-scoped read, not a mocked shortcut (CR-11's
    stated acceptance criterion)."""
    import json

    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row
    from psycopg.types.json import Json

    from backend.audit_store import fetch_active_definition, load_v8_definition
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
            (
                str(uuid.uuid4()), org_custom, "Custom (CR-11 test)", 2,
                Json(custom_def),
            ),
        )

        untouched_result = fetch_active_definition(conn, org_id=org_untouched)
        assert untouched_result == load_v8_definition()

        custom_result = fetch_active_definition(conn, org_id=org_custom)
        assert json.dumps(custom_result, sort_keys=True) == json.dumps(custom_def, sort_keys=True)
        assert custom_result != load_v8_definition()
    finally:
        conn.rollback()
        conn.close()
