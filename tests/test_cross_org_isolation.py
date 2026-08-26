"""CL-11: Org A cannot reach Org B data (application SQL + RLS).

Uses db.connection() (SET LOCAL ROLE callproof_app), not a raw postgres
connect — postgres bypasses RLS and would make every check pass for the
wrong reason. CI provides Postgres and fails the build if any assertion fails.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

from backend.audit_store import seed_legacy_rubric, upsert_audit
from backend.db import connection
from backend.db_url import database_url
from backend.org_ids import org_scope
from backend.paths import ENV_FILE, ROOT
from tests.conftest import authorize

RLS_TABLES = (
    "orgs", "calls", "segments", "audits", "rubrics", "api_usage", "org_credentials",
)


def _in_ci() -> bool:
    return bool(os.getenv("CI") or os.getenv("GITHUB_ACTIONS"))


def _require_database_url() -> str:
    load_dotenv(ENV_FILE)
    raw = database_url()
    if raw:
        return raw
    if _in_ci():
        pytest.fail("DATABASE_URL is required in CI for isolation tests")
    pytest.skip("DATABASE_URL not set")


@dataclass(frozen=True)
class TwoOrgs:
    org_a: str
    org_b: str
    call_a: int
    call_b: int
    segment_a: int
    segment_b: int
    audit_a: str
    audit_b: str
    rubric_a: str
    rubric_b: str


def _seed_org(org_id: str, *, name: str, rubric_id: str, identity: str) -> tuple[int, int, str]:
    with org_scope(org_id):
        with connection() as conn:
            conn.execute(
                "INSERT INTO orgs (id, name) VALUES (%s, %s)",
                (org_id, name),
            )
            seed_legacy_rubric(conn, org_id=org_id, rubric_id=rubric_id)
            call = conn.execute(
                """
                INSERT INTO calls (org_id, audio_url, status, filename)
                VALUES (%s, %s, 'completed', %s)
                RETURNING id
                """,
                (org_id, identity, f"{name}.mp3"),
            ).fetchone()
            call_id = int(call["id"])
            seg = conn.execute(
                """
                INSERT INTO segments (
                    org_id, call_id, seq, speaker, channel, "start", "end", text
                )
                VALUES (%s, %s, 0, 'speaker_1', 0, 0.0, 1.0, 'hello')
                RETURNING id
                """,
                (org_id, call_id),
            ).fetchone()
            audit_id = upsert_audit(
                conn,
                call_id=call_id,
                findings={"score": 80, "grade": "A", "cl11": name},
                engine_version="cl11",
                org_id=org_id,
                rubric_id=rubric_id,
            )
            conn.execute(
                """
                INSERT INTO api_usage (
                    org_id, provider, method, path, status, units, created_at
                )
                VALUES (%s, 'test', 'GET', '/cl11', 200, 1, now())
                """,
                (org_id,),
            )
            conn.execute(
                """
                INSERT INTO org_credentials (org_id, provider, key_suffix, updated_at)
                VALUES (%s, 'justcall', %s, now())
                """,
                (org_id, name[-4:]),
            )
    return call_id, int(seg["id"]), audit_id


def _wipe(org_ids: tuple[str, str]) -> None:
    with connection(bypass_rls=True) as conn:
        for org_id in org_ids:
            conn.execute("DELETE FROM org_credentials WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM api_usage WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM audits WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM segments WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM calls WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
            conn.execute("DELETE FROM orgs WHERE id = %s", (org_id,))


@pytest.fixture(scope="module")
def two_orgs() -> Iterator[TwoOrgs]:
    """AC1: two orgs, each with a call, segment, audit, rubric, and usage row."""
    _require_database_url()
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    rubric_a = str(uuid.uuid4())
    rubric_b = str(uuid.uuid4())
    token = uuid.uuid4().hex
    try:
        call_a, segment_a, audit_a = _seed_org(
            org_a,
            name="cl11-a",
            rubric_id=rubric_a,
            identity=f"test://cl11/{token}/a",
        )
        call_b, segment_b, audit_b = _seed_org(
            org_b,
            name="cl11-b",
            rubric_id=rubric_b,
            identity=f"test://cl11/{token}/b",
        )
        yield TwoOrgs(
            org_a=org_a,
            org_b=org_b,
            call_a=call_a,
            call_b=call_b,
            segment_a=segment_a,
            segment_b=segment_b,
            audit_a=audit_a,
            audit_b=audit_b,
            rubric_a=rubric_a,
            rubric_b=rubric_b,
        )
    finally:
        _wipe((org_a, org_b))


@pytest.mark.isolation
@pytest.mark.parametrize("table", RLS_TABLES)
def test_org_a_gets_zero_org_b_rows(two_orgs: TwoOrgs, table: str) -> None:
    """AC2: as Org A, every tenant table returns zero Org B rows."""
    b = two_orgs.org_b
    with org_scope(two_orgs.org_a):
        with connection() as conn:
            who = conn.execute("SELECT current_user AS u").fetchone()
            assert who["u"] == "callproof_app"
            if table == "orgs":
                row = conn.execute("SELECT id FROM orgs WHERE id = %s", (b,)).fetchone()
                mine = conn.execute(
                    "SELECT id FROM orgs WHERE id = %s", (two_orgs.org_a,),
                ).fetchone()
            elif table == "calls":
                row = conn.execute("SELECT id FROM calls WHERE org_id = %s", (b,)).fetchone()
                mine = conn.execute(
                    "SELECT id FROM calls WHERE id = %s", (two_orgs.call_a,),
                ).fetchone()
            elif table == "segments":
                row = conn.execute(
                    "SELECT id FROM segments WHERE org_id = %s", (b,),
                ).fetchone()
                mine = conn.execute(
                    "SELECT id FROM segments WHERE id = %s", (two_orgs.segment_a,),
                ).fetchone()
            elif table == "audits":
                row = conn.execute(
                    "SELECT id FROM audits WHERE org_id = %s", (b,),
                ).fetchone()
                mine = conn.execute(
                    "SELECT id FROM audits WHERE id = %s", (two_orgs.audit_a,),
                ).fetchone()
            elif table == "rubrics":
                row = conn.execute(
                    "SELECT id FROM rubrics WHERE org_id = %s", (b,),
                ).fetchone()
                mine = conn.execute(
                    "SELECT id FROM rubrics WHERE id = %s", (two_orgs.rubric_a,),
                ).fetchone()
            elif table == "api_usage":
                row = conn.execute(
                    "SELECT id FROM api_usage WHERE org_id = %s", (b,),
                ).fetchone()
                mine = conn.execute(
                    "SELECT id FROM api_usage WHERE org_id = %s LIMIT 1",
                    (two_orgs.org_a,),
                ).fetchone()
            else:
                assert table == "org_credentials"
                row = conn.execute(
                    "SELECT org_id FROM org_credentials WHERE org_id = %s", (b,),
                ).fetchone()
                mine = conn.execute(
                    "SELECT org_id FROM org_credentials WHERE org_id = %s",
                    (two_orgs.org_a,),
                ).fetchone()
            assert row is None, f"{table}: Org A saw Org B rows"
            assert mine is not None, f"{table}: Org A could not see its own row"


@pytest.mark.isolation
def test_org_a_cannot_select_org_b_call_by_id(two_orgs: TwoOrgs) -> None:
    """AC3: direct-ID SQL as Org A does not return Org B's call."""
    with org_scope(two_orgs.org_a):
        with connection() as conn:
            leaked = conn.execute(
                "SELECT * FROM calls WHERE id = %s",
                (two_orgs.call_b,),
            ).fetchone()
            own = conn.execute(
                "SELECT * FROM calls WHERE id = %s",
                (two_orgs.call_a,),
            ).fetchone()
    assert leaked is None
    assert own is not None
    assert int(own["id"]) == two_orgs.call_a


@pytest.mark.isolation
def test_org_a_justcall_status_does_not_show_org_b_suffix(
    two_orgs: TwoOrgs, monkeypatch,
) -> None:
    """org_credentials RLS: Org A status is suffix-only and not Org B's row."""
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=two_orgs.org_a)
    r = client.get("/api/integrations/justcall")
    assert r.status_code == 200
    body = r.json()
    assert "api_key" not in body
    assert "api_secret" not in body
    assert body.get("configured") is True
    assert body.get("key_suffix") == "11-a"
    assert "11-b" not in r.text

    authorize(client, monkeypatch, org_id=two_orgs.org_b)
    other = client.get("/api/integrations/justcall")
    assert other.status_code == 200
    assert other.json().get("key_suffix") == "11-b"
    assert "11-a" not in other.text


@pytest.mark.isolation
def test_org_a_cannot_fetch_org_b_call_via_api(two_orgs: TwoOrgs, monkeypatch) -> None:
    """AC3: HTTP fetch of Org B's call id as Org A is 404, not 200 or 403."""
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=two_orgs.org_a)

    listed = client.get("/api/calls")
    assert listed.status_code == 200
    ids = [int(row["id"]) for row in listed.json()]
    assert two_orgs.call_b not in ids
    assert two_orgs.call_a in ids

    audit = client.get(f"/api/calls/{two_orgs.call_b}/audit")
    assert audit.status_code == 404
    assert audit.status_code != 403

    audio = client.get(f"/api/calls/{two_orgs.call_b}/audio")
    assert audio.status_code == 404

    flagged = client.post(f"/api/calls/{two_orgs.call_b}/flag")
    assert flagged.status_code == 404


def test_ci_runs_isolation_suite() -> None:
    """AC4: the GitHub Actions workflow runs this suite against Postgres."""
    path = ROOT / ".github" / "workflows" / "ci.yml"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "alembic upgrade head" in text
    assert "postgres" in text.lower()
    assert "test_cross_org_isolation" in text or "pytest -q" in text
    assert "DATABASE_URL" in text
