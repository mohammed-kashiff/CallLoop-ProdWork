"""IN-3: /api/integrations/intercom routes — connect redirect, public
callback, status, disconnect. No live Intercom API calls, no real Vault."""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.org_ids import DEFAULT_ORG_ID, bound_org_id

ORG_B = "00000000-0000-4000-8000-000000000002"


def _stub_vault(monkeypatch):
    """In-memory Vault keyed by (org_id, provider). Route tests never hit
    vault.secrets."""
    store: dict[tuple[str, str], dict] = {}

    def put(org_id, provider, data, *, key_suffix=None):
        store[(org_id, provider)] = {"data": data, "suffix": key_suffix}
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

    seen_bound_org_id = {}

    def put_credential(org_id, provider, data, *, key_suffix=None):
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
    client = TestClient(app)

    state = intercom_oauth.make_state(DEFAULT_ORG_ID)
    r = client.get(f"/api/integrations/intercom/callback?code=abc&state={state}")
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
