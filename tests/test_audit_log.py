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
