"""CL-9: org_id from JWT only; every data query/insert is tenant-scoped."""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from backend.auth import org_id_from_request
from backend.org_ids import DEFAULT_ORG_ID, integration_org_id, parse_org_id
from backend.paths import ROOT
from tests.conftest import authorize

BACKEND = ROOT / "backend"


def test_org_id_from_request_ignores_query_string():
    from starlette.requests import Request

    other = str(uuid.uuid4())
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/calls",
            "raw_path": b"/api/calls",
            "query_string": f"org_id={other}".encode(),
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("test", 80),
        }
    )
    request.state.org_id = DEFAULT_ORG_ID
    assert org_id_from_request(request) == DEFAULT_ORG_ID
    assert other not in (org_id_from_request(request),)


def test_me_ignores_org_id_query_param(monkeypatch):
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    forged = str(uuid.uuid4())
    r = client.get("/api/me", params={"org_id": forged})
    assert r.status_code == 200
    assert r.json()["org_id"] == DEFAULT_ORG_ID
    assert r.json()["org_id"] != forged


def test_api_never_reads_client_org_id():
    text = (BACKEND / "api.py").read_text(encoding="utf-8")
    assert "DEFAULT_ORG_ID" not in text
    assert "org_id_from_request" in text

    # Platform-admin exception: /api/admin/* is gated by require_platform_admin
    # before a TARGET org_id is read. Do not copy this into tenant routes.
    start = text.index("# --- platform admin")
    end = text.index("# --- end platform admin")
    admin_region = text[start:end]
    outside = text[:start] + text[end:]

    lowered = outside.lower()
    assert 'query_params.get("org_id")' not in lowered
    assert "query_params['org_id']" not in lowered
    assert "body.org_id" not in lowered
    assert "org_id: str = query" not in lowered

    assert "require_platform_admin" in admin_region
    assert "body.org_id" in admin_region
    assert admin_region.count("require_platform_admin") >= 4


def test_data_sql_filters_org_id():
    """Reads/writes in handlers and transcript spine include org_id binds."""
    api = (BACKEND / "api.py").read_text(encoding="utf-8")
    transcribe = (BACKEND / "transcribe.py").read_text(encoding="utf-8")
    assert "WHERE id = %s AND org_id = %s" in api or "AND org_id = %s" in api
    assert "WHERE c.org_id = %s" in api
    assert "DELETE FROM audits WHERE org_id = %s" in api
    assert "DELETE FROM calls WHERE org_id = %s" in api
    assert "WHERE org_id = %s AND audio_url" in transcribe
    assert "WHERE id = %s AND org_id = %s" in transcribe


def test_isolation_checklist_rule_exists():
    path = ROOT / ".cursor" / "rules" / "org-isolation.mdc"
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "org_id" in text
    assert "JWT" in text or "jwt" in text.lower()
    assert "never" in text.lower()
    assert "query" in text.lower()
    assert "INSERT" in text or "insert" in text.lower()


def test_parse_org_id_rejects_junk():
    assert parse_org_id("not-a-uuid") is None
    assert parse_org_id("") is None
    assert parse_org_id(DEFAULT_ORG_ID) == DEFAULT_ORG_ID


def test_integration_org_id_comes_from_env_not_payload(monkeypatch):
    monkeypatch.delenv("JUSTCALL_ORG_ID", raising=False)
    assert integration_org_id() == DEFAULT_ORG_ID
    custom = str(uuid.uuid4())
    monkeypatch.setenv("JUSTCALL_ORG_ID", custom)
    assert integration_org_id() == custom
    monkeypatch.setenv("JUSTCALL_ORG_ID", "payload-forgery")
    assert integration_org_id() == DEFAULT_ORG_ID


def test_find_existing_call_does_not_leak_other_org():
    """Live Postgres: same audio identity in org B is invisible to org A."""
    import pytest
    from dotenv import load_dotenv
    from psycopg.rows import dict_row

    from backend.db_url import database_url, psycopg_url
    from backend.org_ids import DEFAULT_ORG_ID
    from backend.paths import ENV_FILE
    from backend.transcribe import find_existing_call

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg

    org_b = str(uuid.uuid4())
    identity = f"test://cl9/{uuid.uuid4().hex}"
    conn = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    conn.autocommit = False
    try:
        conn.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "cl9-b"))
        conn.execute(
            """
            INSERT INTO calls (org_id, audio_url, status)
            VALUES (%s, %s, 'completed')
            """,
            (org_b, identity),
        )
        assert find_existing_call(conn, identity, org_id=org_b) is not None
        assert find_existing_call(conn, identity, org_id=DEFAULT_ORG_ID) is None
    finally:
        conn.rollback()
        conn.close()
