"""TA-24 follow-on: self-serve TICKET rubric builder — the same
customer-facing, owner-gated mechanism rubric_builder.py already proved
out for calls, now for kind="ticket" rows via ticket_rubric_builder.py.
Mirrors test_rubric_builder.py's structure; the live section additionally
proves the activate_rubric_by_name cross-kind bug fixed alongside this
module (audit_store.py, 2026-09-16) actually holds end to end."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID
from tests.conftest import authorize, mint_access_token

ORG_A = DEFAULT_ORG_ID


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, row=None):
        self.row = row
        self.queries: list[tuple] = []

    def execute(self, sql, params=None):
        norm = " ".join(str(sql).split()).upper()
        self.queries.append((norm, params))
        assert "FROM RUBRICS WHERE ORG_ID" in norm
        return _Result([self.row] if self.row else [])


@contextmanager
def _fake_db(monkeypatch, conn):
    @contextmanager
    def _connection(*, bypass_rls=False):
        assert not bypass_rls
        yield conn

    monkeypatch.setattr("backend.ticket_rubric_builder.db.connection", _connection)
    yield conn


# ---------- validation ----------


def test_normalize_dimensions_requires_100_total():
    from fastapi import HTTPException

    from backend.ticket_rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([
            {"kind": "builtin", "id": "problem_diagnosis", "weight": 50},
        ])
    assert exc.value.status_code == 400
    assert "100" in str(exc.value.detail)


def test_normalize_dimensions_accepts_a_builtin_plus_custom_mix():
    from backend.ticket_rubric_builder import _normalize_dimensions

    out = _normalize_dimensions([
        {"kind": "builtin", "id": "problem_diagnosis", "weight": 60},
        {
            "kind": "custom", "name": "Confirmed the SKU",
            "question": "Did the agent confirm the exact SKU before refunding?",
            "weight": 40,
        },
    ])
    assert len(out) == 2
    builtin, custom = out
    assert builtin["id"] == "problem_diagnosis"
    assert builtin["weight"] == 60
    # built-in internals (question/customer_facing_only) are preserved
    # untouched, not reauthored
    assert "method" not in builtin
    assert custom["id"] == "confirmed_the_sku"
    assert "method" not in custom
    assert custom["question"] == "Did the agent confirm the exact SKU before refunding?"
    assert custom["weight"] == 40


def test_normalize_dimensions_rejects_unknown_builtin_id():
    from fastapi import HTTPException

    from backend.ticket_rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([{"kind": "builtin", "id": "made_up", "weight": 100}])
    assert exc.value.status_code == 400


def test_normalize_dimensions_rejects_custom_without_question():
    from fastapi import HTTPException

    from backend.ticket_rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([{"kind": "custom", "name": "X", "question": "", "weight": 100}])
    assert exc.value.status_code == 400


def test_normalize_dimensions_rejects_the_reserved_response_timeliness_id():
    """response_timeliness is always computed deterministically and
    appended outside the weighted score — a team must never be able to
    shadow it with their own weighted dimension of the same id."""
    from backend.ticket_rubric_builder import _normalize_dimensions

    out = _normalize_dimensions([
        {"kind": "custom", "name": "Response Timeliness", "question": "Q?", "weight": 100},
    ])
    assert out[0]["id"] != "response_timeliness"


def test_normalize_dimensions_slugifies_duplicate_names_to_unique_ids():
    from backend.ticket_rubric_builder import _normalize_dimensions

    out = _normalize_dimensions([
        {"kind": "custom", "name": "Greeting", "question": "Did they greet?", "weight": 50},
        {"kind": "custom", "name": "Greeting", "question": "Did they greet warmly?", "weight": 50},
    ])
    ids = [d["id"] for d in out]
    assert len(set(ids)) == 2
    assert ids[0] == "greeting"
    assert ids[1] == "greeting_2"


def test_normalize_dimensions_rejects_too_many():
    from fastapi import HTTPException

    from backend.ticket_rubric_builder import _normalize_dimensions

    items = [
        {"kind": "custom", "name": f"C{i}", "question": "Q?", "weight": 1}
        for i in range(13)
    ]
    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions(items)
    assert exc.value.status_code == 400


def test_normalize_dimensions_carries_customer_facing_only_for_a_custom_dimension():
    from backend.ticket_rubric_builder import _normalize_dimensions

    out = _normalize_dimensions([
        {"kind": "custom", "name": "Apology tone", "question": "Q?", "weight": 100,
         "customer_facing_only": True},
    ])
    assert out[0]["customer_facing_only"] is True


# ---------- current_rubric (read) ----------


def test_current_rubric_describes_the_five_real_dimensions_as_builtin(monkeypatch):
    from backend import ticket_rubric
    from backend.ticket_rubric_builder import current_rubric

    monkeypatch.setattr(
        "backend.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {
            "id": "rubric-id", "name": ticket_rubric.TICKET_QA_RUBRIC_NAME, "version": 1,
            "dimensions": ticket_rubric.get_default_ticket_rubric(),
        },
    )
    out = current_rubric(ORG_A)
    assert out["name"] == ticket_rubric.TICKET_QA_RUBRIC_NAME
    assert len(out["dimensions"]) == len(ticket_rubric.TICKET_QA_DIMENSIONS)
    assert all(d["kind"] == "builtin" for d in out["dimensions"])
    assert len(out["available_builtins"]) == len(ticket_rubric.TICKET_QA_DIMENSIONS)


def test_current_rubric_describes_a_custom_mix(monkeypatch):
    from backend.ticket_rubric_builder import current_rubric

    monkeypatch.setattr(
        "backend.ticket_rubric.ensure_ticket_rubric",
        lambda org_id: {
            "id": "rubric-id", "name": "Support Ticket Rubric", "version": 2,
            "dimensions": [
                {"id": "problem_diagnosis", "name": "Problem Diagnosis", "weight": 70,
                 "question": "Diagnosed correctly?"},
                {"id": "confirmed_the_sku", "name": "Confirmed the SKU", "weight": 30,
                 "question": "Confirmed the SKU?"},
            ],
        },
    )
    out = current_rubric(ORG_A)
    assert out["name"] == "Support Ticket Rubric"
    kinds = {d["kind"] for d in out["dimensions"]}
    assert kinds == {"builtin", "custom"}
    custom = next(d for d in out["dimensions"] if d["kind"] == "custom")
    assert custom["question"] == "Confirmed the SKU?"


def test_current_rubric_rejects_bad_org_id():
    from fastapi import HTTPException

    from backend.ticket_rubric_builder import current_rubric

    with pytest.raises(HTTPException) as exc:
        current_rubric("not-a-uuid")
    assert exc.value.status_code == 400


# ---------- routes ----------


def test_get_ticket_rubric_builder_route_available_to_any_org_member(monkeypatch):
    seen: list[str] = []

    def _current(org_id):
        seen.append(org_id)
        return {"org_id": org_id, "dimensions": [], "available_builtins": []}

    monkeypatch.setattr("backend.ticket_score_api.ticket_rubric_builder.current_rubric", _current)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/tickets/rubric/builder")
    assert r.status_code == 200
    assert seen == [ORG_A]


def test_save_ticket_rubric_route_403_for_a_member_not_the_owner(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id),
        ),
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.post(
        "/api/tickets/rubric/builder",
        json={"dimensions": [{"kind": "builtin", "id": "problem_diagnosis", "weight": 100}]},
    )
    assert r.status_code == 403


def test_save_ticket_rubric_route_allows_the_owner_and_forwards_dimensions(monkeypatch):
    seen: list[tuple] = []

    def _save(org_id, dimensions, *, changed_by):
        seen.append((org_id, dimensions, changed_by))
        return {"org_id": org_id, "rubric_id": "rid", "version": 1,
                "updated_at": None, "dimensions": dimensions, "available_builtins": []}

    monkeypatch.setattr("backend.ticket_score_api.ticket_rubric_builder.save_rubric", _save)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)  # authorize() always grants "owner"
    r = client.post(
        "/api/tickets/rubric/builder",
        json={"dimensions": [{"kind": "builtin", "id": "problem_diagnosis", "weight": 100}]},
    )
    assert r.status_code == 200
    assert seen[0][0] == ORG_A
    assert seen[0][2] == "tester@example.com"


def test_activate_ticket_rubric_route_owner_only(monkeypatch):
    from backend.auth import Membership
    from backend.api import app

    uid = str(uuid.uuid4())
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            ORG_A, "member", str(user_id),
        ),
    )
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    r = client.post("/api/tickets/rubrics/Support%20Ticket%20Rubric/activate")
    assert r.status_code == 403


def test_get_ticket_rubric_by_name_route_404_for_unknown_name(monkeypatch):
    from backend.api import app

    def _get(org_id, name):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"No saved ticket rubric named {name!r}.")

    monkeypatch.setattr("backend.ticket_score_api.ticket_rubric_builder.get_rubric", _get)
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/tickets/rubrics/Nonexistent")
    assert r.status_code == 404


# ---------- live Postgres: the two rubric engines must not step on each other ----------


def test_saving_and_activating_a_ticket_rubric_does_not_touch_the_orgs_call_rubric_live():
    """The mirror image of test_rubric_builder.py's TA-35 live test: this
    time exercising the ticket side, plus activate_rubric_by_name, which
    had its own unguarded cross-kind deactivation bug (found and fixed
    alongside this module, 2026-09-16) that TA-35's fix session missed —
    it deactivated whatever else was active for the org with no kind
    filter at all, unlike save_named_rubric. Reproduces both a save and
    an activate through the self-serve ticket builder and confirms an
    org's active call rubric survives both."""
    from dotenv import load_dotenv

    from backend.db_url import database_url, psycopg_url
    from backend.paths import ENV_FILE

    load_dotenv(ENV_FILE)
    raw = database_url()
    if not raw:
        pytest.skip("DATABASE_URL not set")

    import psycopg
    from psycopg.rows import dict_row

    from backend import rubric_builder, ticket_rubric, ticket_rubric_builder

    admin = psycopg.connect(psycopg_url(raw), row_factory=dict_row, prepare_threshold=0)
    org_id = str(uuid.uuid4())
    try:
        admin.execute("INSERT INTO orgs (id, name) VALUES (%s, %s)", (org_id, "ticket-builder-live-test"))
        admin.commit()

        # Seed the org's call rubric first — an explicit, named, active
        # library entry, the way a real team would have one.
        rubric_builder.save_rubric(
            org_id,
            [{"kind": "custom", "name": "My check", "question": "Did the agent do the thing?",
              "weight": 100}],
            changed_by="owner@example.com", name="My Call Rubric",
        )
        call_before = rubric_builder.current_rubric(org_id)
        assert call_before["name"] == "My Call Rubric"

        # Save a custom ticket rubric through the self-serve ticket builder.
        saved = ticket_rubric_builder.save_rubric(
            org_id,
            [{"kind": "builtin", "id": "problem_diagnosis", "weight": 60},
             {"kind": "custom", "name": "Confirmed the SKU",
              "question": "Did the agent confirm the SKU?", "weight": 40}],
            changed_by="owner@example.com", name="Support Ticket Rubric",
        )
        assert saved["name"] == "Support Ticket Rubric"

        # The call rubric must be untouched by the ticket save.
        call_after_save = rubric_builder.current_rubric(org_id)
        assert call_after_save["name"] == "My Call Rubric"
        assert call_after_save["rubric_id"] == call_before["rubric_id"]

        # Now activate that same named ticket rubric — the exact
        # activate_rubric_by_name path that had no kind guard at all.
        ticket_rubric_builder.activate_rubric(
            org_id, "Support Ticket Rubric", changed_by="owner@example.com",
        )

        # The call rubric must still be untouched after the activate.
        call_after_activate = rubric_builder.current_rubric(org_id)
        assert call_after_activate["name"] == "My Call Rubric"
        assert call_after_activate["rubric_id"] == call_before["rubric_id"]

        # And the ticket engine's own active rubric must be the one just
        # activated, not fall back to a fresh default.
        active_ticket = ticket_rubric.fetch_active_ticket_rubric(org_id)
        assert active_ticket is not None
        assert active_ticket["name"] == "Support Ticket Rubric"

        # The ticket rubric must never appear in the call rubric library.
        call_listing = rubric_builder.list_rubrics(org_id)
        assert "Support Ticket Rubric" not in {r["name"] for r in call_listing["rubrics"]}
        # And vice versa.
        ticket_listing = ticket_rubric_builder.list_rubrics(org_id)
        assert "My Call Rubric" not in {r["name"] for r in ticket_listing["rubrics"]}
    finally:
        admin.execute("DELETE FROM rubrics WHERE org_id = %s", (org_id,))
        admin.execute("DELETE FROM orgs WHERE id = %s", (org_id,))
        admin.commit()
        admin.close()
