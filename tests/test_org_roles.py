"""AC-56/AC-58: the account owner promotes/demotes their own team members
to/from Manager. org_members has no RLS (0005_rls.py) — every query here
must scope org_id from the caller's own verified membership, never client
input; these tests exercise that scoping directly against real Postgres,
since a fake connection can't catch a missing/incorrect WHERE clause the
way a live query against real rows can (same lesson as TA-19's RLS-grant
bug — see ARCHITECTURE.md)."""

from __future__ import annotations

import uuid

import pytest


def test_set_member_role_rejects_an_unknown_role():
    from fastapi import HTTPException

    from backend.org_roles import set_member_role

    with pytest.raises(HTTPException) as exc:
        set_member_role(
            str(uuid.uuid4()), str(uuid.uuid4()), "owner", changed_by="owner@example.com",
        )
    assert exc.value.status_code == 400
    assert "manager" in str(exc.value.detail).lower()


def test_set_member_role_requires_changed_by():
    from fastapi import HTTPException

    from backend.org_roles import set_member_role

    with pytest.raises(HTTPException) as exc:
        set_member_role(str(uuid.uuid4()), str(uuid.uuid4()), "manager", changed_by="")
    assert exc.value.status_code == 400


def test_set_member_role_rejects_bad_org_id():
    from fastapi import HTTPException

    from backend.org_roles import set_member_role

    with pytest.raises(HTTPException) as exc:
        set_member_role("not-a-uuid", str(uuid.uuid4()), "manager", changed_by="owner@example.com")
    assert exc.value.status_code == 400


def test_list_team_rejects_bad_org_id():
    from fastapi import HTTPException

    from backend.org_roles import list_team

    with pytest.raises(HTTPException) as exc:
        list_team("not-a-uuid")
    assert exc.value.status_code == 400


# ---------- live Postgres ----------


def _live_conn():
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)


def test_promote_then_demote_a_member_round_trips_live():
    from backend import org_roles

    admin = _live_conn()
    org_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "org-roles-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, owner_id),
        )
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'member')",
            (org_id, member_id),
        )
        admin.commit()

        team = org_roles.list_team(org_id)
        assert {m["user_id"] for m in team["members"]} == {owner_id, member_id}
        assert next(m for m in team["members"] if m["user_id"] == member_id)["role"] == "member"

        promoted = org_roles.set_member_role(
            org_id, member_id, "manager", changed_by="owner@example.com",
        )
        assert promoted["role"] == "manager"
        assert promoted["changed"] is True

        team = org_roles.list_team(org_id)
        assert next(m for m in team["members"] if m["user_id"] == member_id)["role"] == "manager"

        demoted = org_roles.set_member_role(
            org_id, member_id, "member", changed_by="owner@example.com",
        )
        assert demoted["role"] == "member"
        assert demoted["changed"] is True

        # AC-66: both the promote and the demote wrote a durable audit_log
        # row, not just the org_member_role_changed log line.
        rows = admin.execute(
            "SELECT action, target_id, before, after, actor_email FROM audit_log "
            "WHERE org_id = %s ORDER BY created_at",
            (org_id,),
        ).fetchall()
        assert [r["action"] for r in rows] == ["member.role_changed", "member.role_changed"]
        assert rows[0]["before"] == {"role": "member"}
        assert rows[0]["after"] == {"role": "manager"}
        assert rows[1]["before"] == {"role": "manager"}
        assert rows[1]["after"] == {"role": "member"}
        assert all(r["target_id"] == member_id for r in rows)
        assert all(r["actor_email"] == "owner@example.com" for r in rows)
    finally:
        admin.execute("DELETE FROM audit_log WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_setting_the_same_role_again_is_a_no_op_reported_as_unchanged_live():
    from backend import org_roles

    admin = _live_conn()
    org_id = str(uuid.uuid4())
    member_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "org-roles-noop-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'member')",
            (org_id, member_id),
        )
        admin.commit()

        out = org_roles.set_member_role(org_id, member_id, "member", changed_by="owner@example.com")
        assert out["changed"] is False
        assert out["role"] == "member"
    finally:
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_cannot_change_the_owners_own_role_live():
    from fastapi import HTTPException

    from backend import org_roles

    admin = _live_conn()
    org_id = str(uuid.uuid4())
    owner_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "org-roles-owner-guard-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'owner')",
            (org_id, owner_id),
        )
        admin.commit()

        with pytest.raises(HTTPException) as exc:
            org_roles.set_member_role(org_id, owner_id, "manager", changed_by="owner@example.com")
        assert exc.value.status_code == 400

        row = admin.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s", (org_id, owner_id),
        ).fetchone()
        assert row["role"] == "owner"
    finally:
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()


def test_set_member_role_404s_for_a_member_in_a_different_org_live():
    """The real tenant-isolation test, since org_members has no RLS: a
    target_user_id that exists but belongs to a DIFFERENT org must never
    be reachable through this org's own set_member_role call."""
    from fastapi import HTTPException

    from backend import org_roles

    admin = _live_conn()
    org_a = str(uuid.uuid4())
    org_b = str(uuid.uuid4())
    member_in_b = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_a, "org-roles-tenant-a-live-test"))
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_b, "org-roles-tenant-b-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'member')",
            (org_b, member_in_b),
        )
        admin.commit()

        with pytest.raises(HTTPException) as exc:
            org_roles.set_member_role(org_a, member_in_b, "manager", changed_by="owner@example.com")
        assert exc.value.status_code == 404

        row = admin.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s", (org_b, member_in_b),
        ).fetchone()
        assert row["role"] == "member"
    finally:
        admin.execute("DELETE FROM org_members WHERE org_id IN (%s, %s)", (org_a, org_b))
        admin.execute("DELETE FROM orgs WHERE id IN (%s, %s)", (org_a, org_b))
        admin.commit()
        admin.close()


# ---------- routes ----------


def test_get_team_route_403_for_a_manager_not_the_owner(monkeypatch):
    """AC-58: promote/demote (and the team list itself) stays owner-only —
    a Manager must not be able to see or change teammates' roles."""
    from backend.auth import Membership
    from backend.api import app
    from fastapi.testclient import TestClient
    from tests.conftest import mint_access_token

    uid = str(uuid.uuid4())
    org_id = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            org_id, "manager", str(user_id),
        ),
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.get("/api/team")
    assert r.status_code == 403
    r = client.post(f"/api/team/{uuid.uuid4()}/role", json={"role": "manager"})
    assert r.status_code == 403


def test_get_team_route_allows_the_owner_and_forwards_org_id(monkeypatch):
    from backend.api import app
    from fastapi.testclient import TestClient
    from tests.conftest import authorize

    org_id = str(uuid.uuid4())
    seen: list[str] = []

    def _list_team(oid):
        seen.append(oid)
        return {"org_id": oid, "members": []}

    monkeypatch.setattr("backend.api.org_roles.list_team", _list_team)
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=org_id)  # authorize() always grants "owner"
    r = client.get("/api/team")
    assert r.status_code == 200
    assert seen == [org_id]


def test_set_team_member_role_route_allows_the_owner_and_forwards_args(monkeypatch):
    from backend.api import app
    from fastapi.testclient import TestClient
    from tests.conftest import authorize

    org_id = str(uuid.uuid4())
    target = str(uuid.uuid4())
    seen: list[tuple] = []

    def _set_role(oid, target_user_id, role, *, changed_by):
        seen.append((oid, target_user_id, role, changed_by))
        return {"org_id": oid, "user_id": target_user_id, "role": role, "changed": True}

    monkeypatch.setattr("backend.api.org_roles.set_member_role", _set_role)
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=org_id)
    r = client.post(f"/api/team/{target}/role", json={"role": "manager"})
    assert r.status_code == 200
    assert seen == [(org_id, target, "manager", "tester@example.com")]


def test_org_members_role_check_constraint_allows_manager_live():
    """AC-57's migration: the widened CHECK must accept 'manager' and still
    reject anything outside the three-role set."""
    admin = _live_conn()
    org_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "role-check-live-test"))
        admin.execute(
            "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'manager')",
            (org_id, user_id),
        )
        admin.commit()

        row = admin.execute(
            "SELECT role FROM org_members WHERE org_id = %s AND user_id = %s", (org_id, user_id),
        ).fetchone()
        assert row["role"] == "manager"

        admin.rollback()
        import psycopg
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(
                "INSERT INTO org_members (org_id, user_id, role) VALUES (%s, %s, 'superadmin')",
                (org_id, str(uuid.uuid4())),
            )
        admin.rollback()
    finally:
        admin.execute("DELETE FROM org_members WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
