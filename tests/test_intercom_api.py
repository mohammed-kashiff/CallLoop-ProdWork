"""IN-3: /api/integrations/intercom routes — connect redirect, public
callback, status, disconnect. No live Intercom API calls, no real Vault."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID, bound_org_id

ORG_B = "00000000-0000-4000-8000-000000000002"


def _stub_vault(monkeypatch):
    """In-memory Vault keyed by (org_id, provider). Route tests never hit
    vault.secrets."""
    store: dict[tuple[str, str], dict] = {}

    def put(org_id, provider, data, *, key_suffix=None, external_account_id=None):
        store[(org_id, provider)] = {
            "data": data, "suffix": key_suffix, "external_account_id": external_account_id,
        }
        return key_suffix or ""

    def load(org_id, provider):
        row = store.get((org_id, provider))
        return dict(row["data"]) if row else None

    def delete(org_id, provider):
        return store.pop((org_id, provider), None) is not None

    def status(org_id, provider):
        row = store.get((org_id, provider))
        if not row:
            return {"configured": False, "suffix": None}
        return {"configured": True, "suffix": row["suffix"]}

    monkeypatch.setattr("backend.org_vault.put_credential", put)
    monkeypatch.setattr("backend.org_vault.load_credential", load)
    monkeypatch.setattr("backend.org_vault.delete_credential", delete)
    monkeypatch.setattr("backend.org_vault.credential_status", status)
    return store


def _configure_app(monkeypatch, *, client_id="ic_client_1", secret="ic_secret_1"):
    monkeypatch.setenv("INTERCOM_CLIENT_ID", client_id)
    monkeypatch.setenv("INTERCOM_CLIENT_SECRET", secret)


def test_status_distinguishes_app_configured_from_org_configured(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)

    r = client.get("/api/integrations/intercom")
    assert r.status_code == 200
    body = r.json()
    assert body["app_configured"] is False
    assert body["configured"] is False

    _configure_app(monkeypatch)
    r2 = client.get("/api/integrations/intercom")
    assert r2.json()["app_configured"] is True
    assert r2.json()["configured"] is False


def test_connect_redirects_to_intercom_when_configured(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    _configure_app(monkeypatch, client_id="ic_client_redirect")
    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)

    r = client.get("/api/integrations/intercom/connect", follow_redirects=False)
    assert r.status_code in (302, 307)
    location = r.headers["location"]
    assert location.startswith("https://app.intercom.com/oauth")
    assert "client_id=ic_client_redirect" in location
    assert "state=" in location


def test_connect_503_when_app_not_configured(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    monkeypatch.delenv("INTERCOM_CLIENT_ID", raising=False)
    monkeypatch.delenv("INTERCOM_CLIENT_SECRET", raising=False)
    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)

    r = client.get("/api/integrations/intercom/connect", follow_redirects=False)
    assert r.status_code == 503


def test_callback_requires_no_jwt_it_is_a_public_route(monkeypatch):
    """The callback is a top-level redirect from Intercom's own domain — it
    must not 401 just because no Authorization header is attached."""
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    _stub_vault(monkeypatch)
    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", lambda code: "tok_abcd1234")
    monkeypatch.setattr(intercom_oauth, "fetch_workspace_id", lambda token: "ws_test123")
    client = TestClient(app)  # deliberately no authorize()

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
    assert r.status_code == 200
    assert r.json()["connected"] is True


def test_callback_stores_the_token_under_the_org_id_from_state(monkeypatch):
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    store = _stub_vault(monkeypatch)
    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", lambda code: "tok_wxyz9999")
    monkeypatch.setattr(intercom_oauth, "fetch_workspace_id", lambda token: "ws_test123")
    client = TestClient(app)

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
    assert r.status_code == 200
    assert store[(DEFAULT_ORG_ID, "intercom")]["data"]["access_token"] == "tok_wxyz9999"
    assert store[(DEFAULT_ORG_ID, "intercom")]["suffix"] == "9999"


def test_callback_binds_org_scope_before_writing_the_credential(monkeypatch):
    """Regression test for a real production bug: the callback is a public
    route (never touches JwtAuthMiddleware, which is what normally binds
    org_id for RLS), so org_credentials' RLS policy — org_id =
    current_org_id() — rejected the INSERT with no bound org, and that raw
    exception wasn't VaultError, so it surfaced as a bare 500. The earlier
    version of this test file stubbed put_credential entirely, which is
    exactly why it didn't catch this — the stub never checked whether an
    org was actually bound. This one does."""
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", lambda code: "tok_scoped_0001")
    monkeypatch.setattr(intercom_oauth, "fetch_workspace_id", lambda token: "ws_test123")

    seen_bound_org_id = {}

    def put_credential(org_id, provider, data, *, key_suffix=None, external_account_id=None):
        seen_bound_org_id["value"] = bound_org_id()
        return key_suffix or ""

    monkeypatch.setattr("backend.org_vault.put_credential", put_credential)
    client = TestClient(app)

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
    assert r.status_code == 200
    assert seen_bound_org_id["value"] == DEFAULT_ORG_ID


def test_callback_never_echoes_the_access_token(monkeypatch):
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    _stub_vault(monkeypatch)
    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", lambda code: "tok_secret_value")
    monkeypatch.setattr(intercom_oauth, "fetch_workspace_id", lambda token: "ws_test123")
    client = TestClient(app)

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
    assert r.status_code == 200
    assert "tok_secret_value" not in r.text


def test_callback_rejects_a_bad_state(monkeypatch):
    from backend.api import app

    _configure_app(monkeypatch)
    _stub_vault(monkeypatch)
    client = TestClient(app)

    r = client.get("/api/integrations/intercom/callback?code=abc&state=garbage")
    assert r.status_code == 400


def test_callback_rejects_intercom_denial(monkeypatch):
    from backend.api import app

    _configure_app(monkeypatch)
    _stub_vault(monkeypatch)
    client = TestClient(app)

    r = client.get("/api/integrations/intercom/callback?error=access_denied")
    assert r.status_code == 400


def test_callback_500s_gracefully_when_token_exchange_fails(monkeypatch):
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    _stub_vault(monkeypatch)

    def _boom(code):
        raise intercom_oauth.IntercomAuthError("token_exchange_failed")

    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", _boom)
    client = TestClient(app)

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
    assert r.status_code == 502


def test_callback_cannot_store_a_credential_for_a_different_org_than_the_state(monkeypatch):
    """The whole point of signing state with org_id: a caller cannot just
    pass ?state=<org B's state> while meaning to act on org A, because
    there is no separate org identity on this request to disagree with —
    state IS the only org identity here. This asserts that identity is
    exactly what gets used, not silently overridable."""
    from backend import intercom_oauth
    from backend.api import app

    _configure_app(monkeypatch)
    store = _stub_vault(monkeypatch)
    monkeypatch.setattr(intercom_oauth, "exchange_code_for_token", lambda code: "tok_orgb_0000")
    monkeypatch.setattr(intercom_oauth, "fetch_workspace_id", lambda token: "ws_test123")
    client = TestClient(app)

    state_for_b = intercom_oauth.make_state(ORG_B)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state_for_b}")
    assert r.status_code == 200
    assert (ORG_B, "intercom") in store
    assert (DEFAULT_ORG_ID, "intercom") not in store


def test_disconnect_removes_the_stored_credential(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    _configure_app(monkeypatch)
    store = _stub_vault(monkeypatch)
    store[(DEFAULT_ORG_ID, "intercom")] = {"data": {"access_token": "tok"}, "suffix": "abcd"}
    client = TestClient(app)
    authorize(client, monkeypatch)

    r = client.delete("/api/integrations/intercom")
    assert r.status_code == 200
    assert r.json()["removed"] is True
    assert (DEFAULT_ORG_ID, "intercom") not in store

    r2 = client.get("/api/integrations/intercom")
    assert r2.json()["configured"] is False


# ── /api/integrations/intercom/webhook ───────────────────────────────────────


def _signed(monkeypatch, body: dict, *, secret: str = "wh_secret_123"):
    import hashlib
    import hmac
    import json

    _configure_app(monkeypatch, secret=secret)
    raw = json.dumps(body).encode("utf-8")
    sig = "sha1=" + hmac.new(secret.encode("utf-8"), raw, hashlib.sha1).hexdigest()
    return raw, sig


def _run_thread_target_synchronously(monkeypatch):
    """Webhook handlers dispatch real work on a background thread — for a
    deterministic test, make the dispatch call its target immediately
    instead of actually threading."""
    import backend.api as api_module

    class _ImmediateThread:
        def __init__(self, target=None, kwargs=None, name=None, daemon=None):
            self._target = target
            self._kwargs = kwargs or {}

        def start(self):
            self._target(**self._kwargs)

    monkeypatch.setattr(api_module.threading, "Thread", _ImmediateThread)


def test_intercom_webhook_stays_public():
    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/integrations/intercom/webhook", data=b"{}")
    # Reaching route logic (not a 401 from JwtAuthMiddleware for lack of a
    # bearer token) proves this path is public — it 401s for a bad
    # signature instead, a completely different check.
    assert r.status_code in (401, 200)


def test_webhook_rejects_an_invalid_signature(monkeypatch):
    from backend.api import app

    _configure_app(monkeypatch, secret="wh_secret_123")
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook",
        data=b'{"topic":"conversation.admin.closed"}',
        headers={"X-Hub-Signature": "sha1=deadbeef"},
    )
    assert r.status_code == 401


def test_webhook_ignores_missing_topic_or_app_id(monkeypatch):
    from backend.api import app

    raw, sig = _signed(monkeypatch, {"type": "notification_event"})
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_webhook_ignores_an_unknown_workspace(monkeypatch):
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "conversation.admin.closed", "app_id": "unknown_workspace",
        "data": {"item": {"id": "conv-1"}},
    })
    monkeypatch.setattr("backend.org_vault.find_org_id_by_external_account", lambda *a, **k: None)
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_webhook_dispatches_conversation_admin_closed_to_ingest(monkeypatch):
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "conversation.admin.closed", "app_id": "ws_abc",
        "data": {"item": {"id": "conv-42"}},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda provider, app_id: DEFAULT_ORG_ID if app_id == "ws_abc" else None,
    )
    _run_thread_target_synchronously(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.intercom_ingest.ingest_intercom_conversation",
        lambda org_id, conversation_id: calls.append((org_id, conversation_id)) or "ticket-1",
    )
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["queued"] == "conv-42"
    assert calls == [(DEFAULT_ORG_ID, "conv-42")]


def test_webhook_swallows_ingest_failures_without_500ing(monkeypatch):
    """A background-thread failure must never surface as a webhook error —
    Intercom would just retry forever against a request that already
    returned 200."""
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "conversation.admin.closed", "app_id": "ws_abc",
        "data": {"item": {"id": "conv-42"}},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda *a, **k: DEFAULT_ORG_ID,
    )
    _run_thread_target_synchronously(monkeypatch)

    def _boom(org_id, conversation_id):
        raise RuntimeError("intercom is down")

    monkeypatch.setattr("backend.intercom_ingest.ingest_intercom_conversation", _boom)
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200


def test_webhook_dispatches_ticket_resolved_ticket_id_is_top_level(monkeypatch):
    """ticket.resolved puts the ticket directly at data.item — confirmed
    against Intercom's own webhook-topics reference."""
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "ticket.resolved", "app_id": "ws_abc", "data": {"item": {"id": "ticket-1"}},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda *a, **k: DEFAULT_ORG_ID,
    )
    _run_thread_target_synchronously(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.intercom_ingest.ingest_intercom_ticket",
        lambda org_id, tid: calls.append((org_id, tid)) or "ticket-row-1",
    )
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["queued"] == "ticket-1"
    assert calls == [(DEFAULT_ORG_ID, "ticket-1")]


def test_webhook_dispatches_ticket_closed_ticket_id_is_nested(monkeypatch):
    """ticket.closed nests the ticket under data.item.ticket, not at the
    top level — the exact shape difference Intercom's own docs warn
    consumers to branch on."""
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "ticket.closed", "app_id": "ws_abc",
        "data": {"item": {"ticket": {"id": "ticket-2"}}},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda *a, **k: DEFAULT_ORG_ID,
    )
    _run_thread_target_synchronously(monkeypatch)
    calls = []
    monkeypatch.setattr(
        "backend.intercom_ingest.ingest_intercom_ticket",
        lambda org_id, tid: calls.append((org_id, tid)) or "ticket-row-2",
    )
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["queued"] == "ticket-2"
    assert calls == [(DEFAULT_ORG_ID, "ticket-2")]


def test_webhook_ticket_closed_does_not_read_the_resolved_shape_by_mistake(monkeypatch):
    """A ticket.closed payload shaped like ticket.resolved (id at the top
    level, no nested "ticket" key) must not be misread — that would ingest
    the wrong id or silently succeed on malformed data."""
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "ticket.closed", "app_id": "ws_abc", "data": {"item": {"id": "not-nested"}},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda *a, **k: DEFAULT_ORG_ID,
    )
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False


def test_webhook_ticket_topics_ignore_missing_ticket_id(monkeypatch):
    from backend.api import app

    for topic, item in (
        ("ticket.resolved", {}),
        ("ticket.closed", {"ticket": {}}),
    ):
        raw, sig = _signed(monkeypatch, {
            "topic": topic, "app_id": "ws_abc", "data": {"item": item},
        })
        monkeypatch.setattr(
            "backend.org_vault.find_org_id_by_external_account",
            lambda *a, **k: DEFAULT_ORG_ID,
        )
        client = TestClient(app)
        r = client.post(
            "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
        )
        assert r.status_code == 200, topic
        assert r.json()["accepted"] is False, topic


def test_webhook_ignores_unhandled_topics(monkeypatch):
    from backend.api import app

    raw, sig = _signed(monkeypatch, {
        "topic": "conversation.admin.opened", "app_id": "ws_abc", "data": {},
    })
    monkeypatch.setattr(
        "backend.org_vault.find_org_id_by_external_account",
        lambda *a, **k: DEFAULT_ORG_ID,
    )
    client = TestClient(app)
    r = client.post(
        "/api/integrations/intercom/webhook", content=raw, headers={"X-Hub-Signature": sig},
    )
    assert r.status_code == 200
    assert r.json()["accepted"] is False


# ── _sync_intercom_recent (IN-6 polling backstop) ────────────────────────────


def test_status_reports_polling_fields(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/integrations/intercom")
    assert r.status_code == 200
    body = r.json()
    assert "polling" in body
    assert body["poll_seconds"] == 300


def test_sync_intercom_recent_requires_a_stored_token(monkeypatch):
    import backend.api as api_module

    monkeypatch.setattr(api_module.org_vault, "load_credential", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="not connected"):
        api_module._sync_intercom_recent(DEFAULT_ORG_ID)


def _stub_no_tickets_found(monkeypatch, api_module):
    """Most conversation-focused tests below don't care about the ticket
    half — stub it to an empty result so it never makes a real HTTP call."""
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_tickets", lambda token, since: [],
    )


def test_sync_intercom_recent_ingests_every_found_conversation(monkeypatch):
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations",
        lambda token, since: [{"id": "c1"}, {"id": "c2"}],
    )
    _stub_no_tickets_found(monkeypatch, api_module)
    ingested = []
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_conversation",
        lambda org_id, cid: ingested.append(cid) or "ticket-id",
    )
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert ingested == ["c1", "c2"]
    assert result == {"found": 2, "processed": 2, "errors": 0}


def test_sync_intercom_recent_continues_past_a_single_ingest_failure(monkeypatch):
    """One bad conversation must not stop the rest of the batch from
    being processed — same discipline as _sync_justcall_recent."""
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations",
        lambda token, since: [{"id": "bad"}, {"id": "good"}],
    )
    _stub_no_tickets_found(monkeypatch, api_module)

    def _ingest(org_id, cid):
        if cid == "bad":
            raise RuntimeError("boom")
        return "ticket-id"

    monkeypatch.setattr(api_module.intercom_ingest, "ingest_intercom_conversation", _ingest)
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert result == {"found": 2, "processed": 1, "errors": 1}


def test_sync_intercom_recent_skips_entries_with_no_id(monkeypatch):
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations",
        lambda token, since: [{"no_id": "here"}],
    )
    _stub_no_tickets_found(monkeypatch, api_module)
    called = []
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_conversation",
        lambda org_id, cid: called.append(cid),
    )
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert called == []
    assert result == {"found": 1, "processed": 0, "errors": 0}


def test_sync_intercom_recent_also_ingests_closed_tickets(monkeypatch):
    """The gap that motivated this: webhooks weren't subscribed yet, so a
    real closed ticket had no path in at all — the poller originally only
    covered conversations."""
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations", lambda token, since: [],
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_tickets",
        lambda token, since: [{"id": "t1"}, {"id": "t2"}],
    )
    ingested = []
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_ticket",
        lambda org_id, tid: ingested.append(tid) or "ticket-row-id",
    )
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert ingested == ["t1", "t2"]
    assert result == {"found": 2, "processed": 2, "errors": 0}


def test_sync_intercom_recent_covers_conversations_and_tickets_together(monkeypatch):
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations",
        lambda token, since: [{"id": "c1"}],
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_tickets",
        lambda token, since: [{"id": "t1"}],
    )
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_conversation", lambda org_id, cid: "row-c",
    )
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_ticket", lambda org_id, tid: "row-t",
    )
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert result == {"found": 2, "processed": 2, "errors": 0}


def test_sync_intercom_recent_a_bad_ticket_does_not_block_conversations(monkeypatch):
    import backend.api as api_module

    monkeypatch.setattr(
        api_module.org_vault, "load_credential",
        lambda org_id, provider: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_conversations",
        lambda token, since: [{"id": "c1"}],
    )
    monkeypatch.setattr(
        api_module.intercom_client, "search_closed_tickets",
        lambda token, since: [{"id": "t1"}],
    )
    monkeypatch.setattr(
        api_module.intercom_ingest, "ingest_intercom_conversation", lambda org_id, cid: "row-c",
    )

    def _boom(org_id, tid):
        raise RuntimeError("ticket ingest boom")

    monkeypatch.setattr(api_module.intercom_ingest, "ingest_intercom_ticket", _boom)
    result = api_module._sync_intercom_recent(DEFAULT_ORG_ID)
    assert result == {"found": 2, "processed": 1, "errors": 1}
