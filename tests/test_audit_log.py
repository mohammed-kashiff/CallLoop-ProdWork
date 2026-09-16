"""AC-63/AC-65: audit_log.record() — the durable audit trail writer.

Live-DB tests prove what a fake-connection unit test can't: that RLS
actually permits the INSERT this module makes, and that the append-only
guarantee genuinely holds (an UPDATE/DELETE against a real row silently
affects zero rows, RLS-denied, not an error — the same lesson migration
0037 already taught this codebase: only a real Postgres round-trip
catches a missing/wrong policy)."""

from __future__ import annotations

import uuid

import pytest


def test_record_never_raises_when_the_db_call_fails(monkeypatch):
    from backend import audit_log

    def _boom(*_a, **_k):
        raise RuntimeError("db is down")

    monkeypatch.setattr("backend.audit_log.db.connection", _boom)
    # Must not raise — a telemetry bug can never break the real action.
    audit_log.record(str(uuid.uuid4()), "test.action")


def test_record_is_a_noop_for_an_invalid_org_id(monkeypatch):
    from backend import audit_log

    called = {"yes": False}
    monkeypatch.setattr(
        "backend.audit_log.db.connection",
        lambda *a, **k: called.update(yes=True),
    )
    audit_log.record("not-a-uuid", "test.action")
    assert called["yes"] is False


def test_record_writes_and_the_append_only_guarantee_holds_live():
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import audit_log
    from backend.db import connection
    from backend.org_ids import org_scope

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute("SELECT to_regclass('public.audit_log') AS t").fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0040_audit_log not applied")

    org_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "audit-log-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, actor_id),
        )
        admin.commit()

        audit_log.record(
            org_id, "member.role_changed",
            target_type="org_member", target_id=actor_id,
            before={"role": "member"}, after={"role": "owner"},
            actor_id=actor_id, actor_email="owner@example.com",
        )

        with org_scope(org_id):
            with connection() as conn:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE org_id = %s", (org_id,),
                ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["action"] == "member.role_changed"
        assert str(row["actor_id"]) == actor_id
        assert row["actor_email"] == "owner@example.com"
        assert row["before"] == {"role": "member"}
        assert row["after"] == {"role": "owner"}

        # Append-only: an UPDATE against a real row must silently affect
        # zero rows (RLS-denied — no UPDATE policy exists), not error and
        # not actually change anything. Same test discipline as migration
        # 0037's own lesson: only a real round-trip proves this.
        with org_scope(org_id):
            with connection() as conn:
                result = conn.execute(
                    "UPDATE audit_log SET action = 'tampered' WHERE org_id = %s",
                    (org_id,),
                )
                assert result.rowcount == 0
                conn.commit()

        with org_scope(org_id):
            with connection() as conn:
                still = conn.execute(
                    "SELECT action FROM audit_log WHERE org_id = %s", (org_id,),
                ).fetchone()
        assert still["action"] == "member.role_changed"

        # Same for DELETE — an admin must not be able to erase their own trail.
        with org_scope(org_id):
            with connection() as conn:
                result = conn.execute(
                    "DELETE FROM audit_log WHERE org_id = %s", (org_id,),
                )
                assert result.rowcount == 0
                conn.commit()
    finally:
        admin.execute("DELETE FROM audit_log WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_record_reads_actor_from_bound_context_when_not_passed_explicitly():
    """A caller inside a route handler doesn't have to pass actor_id/email
    explicitly — record() falls back to the AC-64 request-scoped context."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import audit_log
    from backend.db import connection
    from backend.org_ids import bind_actor_email, bind_user_id, org_scope, reset_actor_email, reset_user_id

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute("SELECT to_regclass('public.audit_log') AS t").fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0040_audit_log not applied")

    org_id = str(uuid.uuid4())
    actor_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "audit-log-live-test-2"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, actor_id),
        )
        admin.commit()

        uid_token = bind_user_id(actor_id)
        email_token = bind_actor_email("bound-actor@example.com")
        try:
            audit_log.record(org_id, "rubric.activated", target_type="rubric", target_id="Ticket QA")
        finally:
            reset_actor_email(email_token)
            reset_user_id(uid_token)

        with org_scope(org_id):
            with connection() as conn:
                row = conn.execute(
                    "SELECT actor_id, actor_email FROM audit_log WHERE org_id = %s", (org_id,),
                ).fetchone()
        assert str(row["actor_id"]) == actor_id
        assert row["actor_email"] == "bound-actor@example.com"
    finally:
        admin.execute("DELETE FROM audit_log WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_list_for_org_is_newest_first_and_org_scoped():
    """AC-69: the org-scoped Activity Log read — RLS-scoped like every
    other org read, never another org's rows."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import audit_log

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute("SELECT to_regclass('public.audit_log') AS t").fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0040_audit_log not applied")

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_a, "audit-list-a"))
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "audit-list-b"))
        admin.commit()

        audit_log.record(org_a, "test.first")
        audit_log.record(org_a, "test.second")
        audit_log.record(org_b, "test.other_org")

        rows = audit_log.list_for_org(org_a)
        assert [r["action"] for r in rows] == ["test.second", "test.first"]
        assert all("org_id" not in r for r in rows)  # single-org view, org_id is implicit
    finally:
        admin.execute("DELETE FROM audit_log WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM orgs WHERE id IN (%s, %s)", (org_a, org_b))
        admin.commit()
        admin.close()


def test_list_for_org_empty_for_invalid_org_id():
    from backend import audit_log

    assert audit_log.list_for_org("not-a-uuid") == []


def test_list_all_spans_every_org_and_includes_org_id():
    """AC-69: the platform-admin cross-org view — bypass_rls, narrowly
    scoped to this one read, same pattern as org_vault's directory lookup."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import audit_log

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    exists = admin.execute("SELECT to_regclass('public.audit_log') AS t").fetchone()
    if not exists or not exists["t"]:
        admin.close()
        pytest.skip("0040_audit_log not applied")

    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_a, "audit-all-a"))
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "audit-all-b"))
        admin.commit()

        audit_log.record(org_a, "test.from_a")
        audit_log.record(org_b, "test.from_b")

        rows = audit_log.list_all(limit=500)
        seen_orgs = {r["org_id"] for r in rows if r["action"] in ("test.from_a", "test.from_b")}
        assert seen_orgs == {org_a, org_b}
    finally:
        admin.execute("DELETE FROM audit_log WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM orgs WHERE id IN (%s, %s)", (org_a, org_b))
        admin.commit()
        admin.close()


def test_audit_log_route_requires_owner_or_manager(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from backend.auth import Membership
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            "00000000-0000-4000-8000-000000000001", "member", str(user_id),
        ),
    )
    r = client.get("/api/audit-log")
    assert r.status_code == 403


def test_audit_log_route_returns_the_callers_own_org_rows(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)  # conftest's authorize() defaults to role=owner
    monkeypatch.setattr(
        "backend.api.audit_log.list_for_org",
        lambda org_id, limit=200: [{"action": "test.route", "org_id_seen": org_id}],
    )
    r = client.get("/api/audit-log")
    assert r.status_code == 200
    assert r.json()["rows"][0]["action"] == "test.route"


def test_admin_audit_log_route_requires_platform_admin(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/admin/audit-log")
    assert r.status_code == 403


def test_admin_audit_log_route_returns_every_orgs_rows(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app
    from tests.conftest import authorize

    client = TestClient(app)
    authorize(client, monkeypatch)
    monkeypatch.setattr("backend.api.auth.require_platform_admin", lambda request: None)
    monkeypatch.setattr(
        "backend.api.audit_log.list_all",
        lambda limit=200: [{"action": "test.cross_org"}],
    )
    r = client.get("/api/admin/audit-log")
    assert r.status_code == 200
    assert r.json()["rows"][0]["action"] == "test.cross_org"
