"""Self-serve rubric builder: customer-facing, owner-gated, separate from
Command Center's admin-only reweighting tool (CR-9..CR-15, untouched)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

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

    monkeypatch.setattr("backend.rubric_builder.db.connection", _connection)
    yield conn


# ---------- validation ----------


def test_normalize_dimensions_requires_100_total(monkeypatch):
    from fastapi import HTTPException
    import pytest

    from backend.rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([
            {"kind": "builtin", "id": "resolution_effectiveness", "weight": 50},
        ])
    assert exc.value.status_code == 400
    assert "100" in str(exc.value.detail)


def test_normalize_dimensions_accepts_a_builtin_plus_custom_mix():
    from backend.rubric_builder import _normalize_dimensions

    out = _normalize_dimensions([
        {"kind": "builtin", "id": "resolution_effectiveness", "weight": 60},
        {
            "kind": "custom", "name": "Confirmed Callback Number",
            "question": "Did the agent confirm the callback number?",
            "weight": 40,
        },
    ])
    assert len(out) == 2
    builtin, custom = out
    assert builtin["id"] == "resolution_effectiveness"
    assert builtin["weight"] == 60
    # built-in internals (method/logic) are preserved untouched, not reauthored
    assert builtin["method"] == "llm"
    assert custom["id"] == "confirmed_callback_number"
    assert custom["method"] == "custom_llm"
    assert custom["question"] == "Did the agent confirm the callback number?"
    assert custom["weight"] == 40


def test_normalize_dimensions_rejects_unknown_builtin_id():
    from fastapi import HTTPException
    import pytest

    from backend.rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([{"kind": "builtin", "id": "made_up", "weight": 100}])
    assert exc.value.status_code == 400


def test_normalize_dimensions_rejects_custom_without_question():
    from fastapi import HTTPException
    import pytest

    from backend.rubric_builder import _normalize_dimensions

    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions([{"kind": "custom", "name": "X", "question": "", "weight": 100}])
    assert exc.value.status_code == 400


def test_normalize_dimensions_slugifies_duplicate_names_to_unique_ids():
    from backend.rubric_builder import _normalize_dimensions

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
    import pytest

    from backend.rubric_builder import _normalize_dimensions

    items = [
        {"kind": "custom", "name": f"C{i}", "question": "Q?", "weight": 1}
        for i in range(13)
    ]
    with pytest.raises(HTTPException) as exc:
        _normalize_dimensions(items)
    assert exc.value.status_code == 400


# ---------- current_rubric (read) ----------


def test_current_rubric_legacy_source_lists_all_four_builtins_as_builtin(monkeypatch):
    from backend.rubric_builder import current_rubric

    conn = _FakeConn(row=None)
    with _fake_db(monkeypatch, conn):
        out = current_rubric(ORG_A)
    assert out["source"] == "legacy"
    assert len(out["dimensions"]) == 4
    assert all(d["kind"] == "builtin" for d in out["dimensions"])
    assert len(out["available_builtins"]) == 4


def test_current_rubric_custom_source_reports_the_saved_mix(monkeypatch):
    from backend.rubric_builder import _normalize_dimensions, _wrap_definition, current_rubric

    dims = _normalize_dimensions([
        {"kind": "builtin", "id": "active_listening", "weight": 70},
        {"kind": "custom", "name": "Upsell attempt", "question": "Did they try to upsell?", "weight": 30},
    ])
    definition = _wrap_definition(dims)
    conn = _FakeConn(row={
        "id": str(uuid.uuid4()), "name": "Sales calls", "version": 2, "definition": definition,
        "updated_at": datetime(2026, 4, 1, tzinfo=timezone.utc),
    })
    with _fake_db(monkeypatch, conn):
        out = current_rubric(ORG_A)
    assert out["source"] == "custom"
    assert out["name"] == "Sales calls"
    kinds = {d["kind"] for d in out["dimensions"]}
    assert kinds == {"builtin", "custom"}
    custom = next(d for d in out["dimensions"] if d["kind"] == "custom")
    assert custom["question"] == "Did they try to upsell?"


def test_current_rubric_rejects_bad_org_id():
    from fastapi import HTTPException
    import pytest

    from backend.rubric_builder import current_rubric

    with pytest.raises(HTTPException) as exc:
        current_rubric("not-a-uuid")
    assert exc.value.status_code == 400


# ---------- routes ----------


def test_get_rubric_route_available_to_any_org_member(monkeypatch):
    seen: list[str] = []

    def _current(org_id):
        seen.append(org_id)
        return {"org_id": org_id, "source": "legacy", "dimensions": [], "available_builtins": []}

    monkeypatch.setattr("backend.api.rubric_builder.current_rubric", _current)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)
    r = client.get("/api/rubric")
    assert r.status_code == 200
    assert seen == [ORG_A]


def test_save_rubric_route_403_for_a_member_not_the_owner(monkeypatch):
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
        "/api/rubric",
        json={"dimensions": [{"kind": "builtin", "id": "active_listening", "weight": 100}]},
    )
    assert r.status_code == 403


def test_save_rubric_route_allows_the_owner_and_forwards_dimensions(monkeypatch):
    seen: list[tuple] = []

    def _save(org_id, dimensions, *, changed_by):
        seen.append((org_id, dimensions, changed_by))
        return {"org_id": org_id, "source": "custom", "rubric_id": "rid", "version": 1,
                "updated_at": None, "dimensions": dimensions, "available_builtins": []}

    monkeypatch.setattr("backend.api.rubric_builder.save_rubric", _save)
    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch, org_id=ORG_A)  # authorize() always grants "owner"
    r = client.post(
        "/api/rubric",
        json={"dimensions": [{"kind": "builtin", "id": "active_listening", "weight": 100}]},
    )
    assert r.status_code == 200
    assert seen[0][0] == ORG_A
    assert seen[0][2] == "tester@example.com"
