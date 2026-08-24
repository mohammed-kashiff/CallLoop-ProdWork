"""CL-6: audits against live Postgres. Skips when DATABASE_URL is unset."""

from __future__ import annotations

import uuid

import pytest
from dotenv import load_dotenv
from psycopg.rows import dict_row

from backend.audit_store import (
    LEGACY_RUBRIC_NAME,
    LEGACY_RUBRIC_VERSION,
    decode_findings,
    fetch_by_id,
    fetch_history,
    fetch_latest_for_rubric,
    parse_scorecard,
    seed_legacy_rubric,
    upsert_audit,
)
from backend.db_url import database_url, psycopg_url
from backend.org_ids import DEFAULT_ORG_ID, DEFAULT_RUBRIC_ID
from backend.paths import ENV_FILE, ROOT

BACKEND = ROOT / "backend"
ORG_B = str(uuid.uuid4())
RUBRIC_B = str(uuid.uuid4())
RUBRIC_OTHER = str(uuid.uuid4())


def _require_url() -> str:
    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")
    return psycopg_url(raw)


@pytest.fixture
def conn():
    import psycopg

    url = _require_url()
    c = psycopg.connect(url, row_factory=dict_row, prepare_threshold=0)
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _insert_call(conn) -> int:
    token = uuid.uuid4().hex
    row = conn.execute(
        """
        INSERT INTO calls (org_id, audio_url, status)
        VALUES (%s, %s, 'completed')
        RETURNING id
        """,
        (DEFAULT_ORG_ID, f"test://cl6/{token}"),
    ).fetchone()
    return int(row["id"])


def test_legacy_rubric_seeded_from_v8_file(conn):
    row = conn.execute(
        "SELECT name, version, definition FROM rubrics WHERE id = %s",
        (DEFAULT_RUBRIC_ID,),
    ).fetchone()
    assert row is not None
    assert row["name"] == LEGACY_RUBRIC_NAME
    assert row["version"] == LEGACY_RUBRIC_VERSION
    definition = row["definition"]
    if isinstance(definition, str):
        import json
        definition = json.loads(definition)
    assert definition.get("rubric_id") == "call_qa_minimal_v8"
    assert "technical_skills" in definition
    assert "soft_skills" in definition


def test_two_rubrics_on_one_call_persist_both(conn):
    call_id = _insert_call(conn)
    conn.execute(
        """
        INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
        VALUES (%s, %s, 'Sales desk', 1, '{}'::jsonb, true)
        """,
        (RUBRIC_OTHER, DEFAULT_ORG_ID),
    )
    upsert_audit(
        conn, call_id=call_id, findings={"score": 80, "grade": "A"}, engine_version="h1",
    )
    upsert_audit(
        conn,
        call_id=call_id,
        findings={"score": 40, "grade": "D"},
        engine_version="h2",
        rubric_id=RUBRIC_OTHER,
    )
    rows = fetch_history(conn, call_id=call_id, org_id=DEFAULT_ORG_ID)
    assert len(rows) == 2
    scores = {str(r["rubric_id"]): r["score"] for r in rows}
    assert scores[DEFAULT_RUBRIC_ID] == 80
    assert scores[RUBRIC_OTHER] == 40


def test_same_rubric_version_upserts_one_row(conn):
    call_id = _insert_call(conn)
    upsert_audit(
        conn, call_id=call_id, findings={"score": 10}, engine_version="h1",
    )
    upsert_audit(
        conn, call_id=call_id, findings={"score": 99}, engine_version="h2",
    )
    rows = conn.execute(
        "SELECT score, engine_version FROM audits WHERE call_id = %s",
        (call_id,),
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["score"] == 99
    assert rows[0]["engine_version"] == "h2"


def test_org_b_does_not_see_org_a_audits(conn):
    call_id = _insert_call(conn)
    conn.execute(
        "INSERT INTO orgs (id, name) VALUES (%s, %s)",
        (ORG_B, "cl6-org-b"),
    )
    seed_legacy_rubric(conn, org_id=ORG_B, rubric_id=RUBRIC_B)
    upsert_audit(
        conn, call_id=call_id, findings={"score": 70}, engine_version="a",
        org_id=DEFAULT_ORG_ID, rubric_id=DEFAULT_RUBRIC_ID,
    )
    latest_b = fetch_latest_for_rubric(
        conn, call_id=call_id, rubric_id=DEFAULT_RUBRIC_ID, org_id=ORG_B,
    )
    assert latest_b is None
    history_b = fetch_history(conn, call_id=call_id, org_id=ORG_B)
    assert history_b == []
    audit_id = str(conn.execute("SELECT id FROM audits WHERE call_id = %s", (call_id,)).fetchone()["id"])
    by_id = fetch_by_id(conn, audit_id=audit_id, org_id=ORG_B)
    assert by_id is None


def test_latest_per_rubric_picks_highest_version(conn):
    call_id = _insert_call(conn)
    upsert_audit(
        conn, call_id=call_id, findings={"score": 1}, engine_version="v1",
        rubric_version=1,
    )
    upsert_audit(
        conn, call_id=call_id, findings={"score": 2}, engine_version="v2",
        rubric_version=2,
    )
    row = fetch_latest_for_rubric(
        conn, call_id=call_id, rubric_id=DEFAULT_RUBRIC_ID, org_id=DEFAULT_ORG_ID,
    )
    scorecard, engine = parse_scorecard(row)
    assert scorecard["score"] == 2
    assert engine == "v2"
    assert decode_findings(row["findings"])["score"] == 2


def test_no_insert_or_replace_in_source():
    hits: list[str] = []
    for path in BACKEND.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "INSERT OR REPLACE" in line.upper() and not line.lstrip().startswith("#"):
                hits.append(f"{path.name}:{i}")
    assert hits == [], hits


def test_no_sqlite3_imports_in_backend():
    hits: list[str] = []
    for path in BACKEND.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "sqlite3" in line and not line.lstrip().startswith("#"):
                hits.append(f"{path.name}:{i}:{line.strip()}")
    assert hits == [], hits


def test_api_audit_reads_name_a_mode():
    text = (BACKEND / "api.py").read_text(encoding="utf-8")
    assert "INSERT OR REPLACE" not in text.upper()
    assert "FROM audits WHERE call_id=?" not in text
    assert "FROM audits WHERE call_id = %s" not in text
    assert "JOIN audits a ON a.call_id = c.id" not in text
    assert "fetch_latest_for_rubric" in text
    assert "latest_default_join_sql" in text
    assert "upsert_audit" in text


def test_list_endpoints_against_postgres(monkeypatch):
    _require_url()
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    for path in (
        "/health",
        "/api/calls",
        "/api/calls/flagged",
        "/api/integrations/justcall",
        "/api/pyai/status",
    ):
        r = client.get(path)
        assert r.status_code == 200, path
    calls = client.get("/api/calls")
    assert isinstance(calls.json(), list)
    flagged = client.get("/api/calls/flagged")
    assert isinstance(flagged.json(), list)
