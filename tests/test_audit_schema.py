"""Multi-rubric audits: surrogate PK + UNIQUE (call_id, rubric_id, rubric_version)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.audit_store import (
    LEGACY_RUBRIC_NAME,
    LEGACY_RUBRIC_VERSION,
    ensure_sqlite_schema,
    fetch_by_id,
    fetch_history,
    fetch_latest_for_rubric,
    parse_scorecard,
    seed_legacy_rubric,
    upsert_audit,
)
from backend.org_ids import DEFAULT_ORG_ID, DEFAULT_RUBRIC_ID

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"

ORG_B = "00000000-0000-4000-8000-000000000002"
RUBRIC_B = "00000000-0000-4000-8000-000000000012"
RUBRIC_OTHER = "00000000-0000-4000-8000-000000000013"


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_sqlite_schema(c)
    return c


def test_legacy_rubric_seeded_from_v8_file():
    c = _conn()
    row = c.execute(
        "SELECT name, version, definition FROM rubrics WHERE id = ?",
        (DEFAULT_RUBRIC_ID,),
    ).fetchone()
    assert row["name"] == LEGACY_RUBRIC_NAME
    assert row["version"] == LEGACY_RUBRIC_VERSION
    definition = json.loads(row["definition"])
    assert definition.get("rubric_id") == "call_qa_minimal_v8"
    assert "technical_skills" in definition
    assert "soft_skills" in definition


def test_two_rubrics_on_one_call_persist_both():
    c = _conn()
    c.execute(
        """
        INSERT INTO rubrics (id, org_id, name, version, definition, is_active)
        VALUES (?, ?, 'Sales desk', 1, '{}', 1)
        """,
        (RUBRIC_OTHER, DEFAULT_ORG_ID),
    )
    upsert_audit(
        c, call_id=1, findings={"score": 80, "grade": "A"}, engine_version="h1",
    )
    upsert_audit(
        c,
        call_id=1,
        findings={"score": 40, "grade": "D"},
        engine_version="h2",
        rubric_id=RUBRIC_OTHER,
    )
    rows = fetch_history(c, call_id=1, org_id=DEFAULT_ORG_ID)
    assert len(rows) == 2
    scores = {r["rubric_id"]: r["score"] for r in rows}
    assert scores[DEFAULT_RUBRIC_ID] == 80
    assert scores[RUBRIC_OTHER] == 40


def test_same_rubric_version_upserts_one_row():
    c = _conn()
    upsert_audit(
        c, call_id=1, findings={"score": 10}, engine_version="h1",
    )
    upsert_audit(
        c, call_id=1, findings={"score": 99}, engine_version="h2",
    )
    rows = c.execute("SELECT score, engine_version FROM audits").fetchall()
    assert len(rows) == 1
    assert rows[0]["score"] == 99
    assert rows[0]["engine_version"] == "h2"


def test_org_b_does_not_see_org_a_audits():
    c = _conn()
    seed_legacy_rubric(c, org_id=ORG_B, rubric_id=RUBRIC_B)
    upsert_audit(
        c, call_id=1, findings={"score": 70}, engine_version="a",
        org_id=DEFAULT_ORG_ID, rubric_id=DEFAULT_RUBRIC_ID,
    )
    latest_b = fetch_latest_for_rubric(
        c, call_id=1, rubric_id=DEFAULT_RUBRIC_ID, org_id=ORG_B,
    )
    assert latest_b is None
    history_b = fetch_history(c, call_id=1, org_id=ORG_B)
    assert history_b == []
    by_id = fetch_by_id(
        c,
        audit_id=c.execute("SELECT id FROM audits").fetchone()["id"],
        org_id=ORG_B,
    )
    assert by_id is None


def test_latest_per_rubric_picks_highest_version():
    c = _conn()
    upsert_audit(
        c, call_id=1, findings={"score": 1}, engine_version="v1",
        rubric_version=1,
    )
    upsert_audit(
        c, call_id=1, findings={"score": 2}, engine_version="v2",
        rubric_version=2,
    )
    row = fetch_latest_for_rubric(
        c, call_id=1, rubric_id=DEFAULT_RUBRIC_ID, org_id=DEFAULT_ORG_ID,
    )
    scorecard, engine = parse_scorecard(row)
    assert scorecard["score"] == 2
    assert engine == "v2"


def test_no_insert_or_replace_in_source():
    hits: list[str] = []
    for path in BACKEND.glob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "INSERT OR REPLACE" in line.upper() and not line.lstrip().startswith("#"):
                hits.append(f"{path.name}:{i}")
    assert hits == [], hits


def test_api_audit_reads_name_a_mode():
    """Every audits read in api.py must choose latest-per-rubric, history, or id."""
    text = (BACKEND / "api.py").read_text(encoding="utf-8")
    assert "INSERT OR REPLACE" not in text.upper()
    assert "FROM audits WHERE call_id=?" not in text
    assert "JOIN audits a ON a.call_id = c.id" not in text
    assert "fetch_latest_for_rubric" in text
    assert "latest_default_join_sql" in text
    assert "upsert_audit" in text
