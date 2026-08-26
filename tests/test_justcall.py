"""JustCall helpers, webhook, and per-org Vault credentials. No live JustCall API calls."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from backend.justcall import extract_completed_id, identity_for, verify_webhook_signature
from backend.org_ids import DEFAULT_ORG_ID
from backend.org_vault import JustCallSecret


ORG_B = "00000000-0000-4000-8000-000000000002"


def _jc_key(suffix: str = "aaaa") -> str:
    return "jc_key_" + suffix * 2


def _jc_secret(suffix: str = "bbbb") -> str:
    return "jc_sec_" + suffix * 2


def _stub_vault(monkeypatch):
    """In-memory Vault. API tests never hit vault.secrets."""
    store: dict[str, dict[str, str]] = {}

    def put(org_id, api_key, api_secret):
        store[org_id] = {
            "api_key": api_key,
            "api_secret": api_secret,
            "suffix": api_key[-4:],
        }
        return api_key[-4:]

    def load(org_id):
        row = store.get(org_id)
        if not row:
            return None
        return JustCallSecret(api_key=row["api_key"], api_secret=row["api_secret"])

    def delete(org_id):
        return store.pop(org_id, None) is not None

    def status(org_id):
        row = store.get(org_id)
        if not row:
            return {"configured": False, "suffix": None}
        return {"configured": True, "suffix": row["suffix"]}

    monkeypatch.setattr("backend.org_vault.put_justcall", put)
    monkeypatch.setattr("backend.org_vault.load_justcall", load)
    monkeypatch.setattr("backend.org_vault.delete_justcall", delete)
    monkeypatch.setattr("backend.org_vault.status", status)
    return store


def test_identity_is_namespaced():
    assert identity_for("12345") == "justcall:12345"


def test_extract_completed_id_from_webhook():
    assert extract_completed_id({
        "type": "call.completed",
        "data": {"id": 99},
    }) == "99"


def test_extract_completed_id_ignores_validation_ping():
    assert extract_completed_id({"type": "url.validation"}) is None
    assert extract_completed_id({"type": "call.initiated", "data": {"id": 1}}) is None


def test_extract_completed_id_from_list_row():
    assert extract_completed_id({"id": "abc-1", "agent_name": "Ada"}) == "abc-1"


def test_webhook_signature_optional_when_secret_unset(monkeypatch):
    monkeypatch.delenv("JUSTCALL_WEBHOOK_SECRET", raising=False)
    assert verify_webhook_signature(b"{}", None) is True


def test_webhook_signature_required_when_secret_set(monkeypatch):
    monkeypatch.setenv("JUSTCALL_WEBHOOK_SECRET", "s3cret")
    assert verify_webhook_signature(b"{}", None) is False
    import hashlib
    import hmac
    sig = hmac.new(b"s3cret", b"{}", hashlib.sha256).hexdigest()
    assert verify_webhook_signature(b"{}", sig) is True
    assert verify_webhook_signature(b"{}", "sha256=" + sig) is True


def test_webhook_validation_returns_200():
    from backend.api import app

    client = TestClient(app)
    r = client.post("/api/integrations/justcall/webhook", json={"type": "url.validation"})
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_status_never_echoes_secrets(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.get("/api/integrations/justcall")
    assert r.status_code == 200
    body = r.json()
    assert "configured" in body
    blob = str(body).lower()
    assert "justcall_api" not in blob
    assert "api_secret" not in blob
    assert "api_key" not in blob


def test_save_update_delete_justcall_via_vault(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    store = _stub_vault(monkeypatch)
    key = _jc_key("wxyz")
    secret = _jc_secret("mnop")
    client = TestClient(app)
    authorize(client, monkeypatch)

    created = client.post(
        "/api/integrations/justcall",
        json={"api_key": key, "api_secret": secret},
    )
    assert created.status_code == 200, created.text
    assert key not in created.text
    assert secret not in created.text
    body = created.json()
    assert body["ok"] is True
    assert body["configured"] is True
    assert body["key_suffix"] == key[-4:]
    assert "api_key" not in body
    assert "api_secret" not in body
    assert store[DEFAULT_ORG_ID]["api_key"] == key

    replacement = _jc_key("zzzz")
    replacement_secret = _jc_secret("yyyy")
    updated = client.post(
        "/api/integrations/justcall",
        json={"api_key": replacement, "api_secret": replacement_secret},
    )
    assert updated.status_code == 200, updated.text
    assert replacement not in updated.text
    assert replacement_secret not in updated.text
    assert updated.json()["key_suffix"] == replacement[-4:]
    assert store[DEFAULT_ORG_ID]["api_key"] == replacement

    listed = client.get("/api/integrations/justcall")
    assert listed.status_code == 200
    assert listed.json()["configured"] is True
    assert listed.json()["key_suffix"] == replacement[-4:]
    assert replacement not in listed.text
    assert replacement_secret not in listed.text

    removed = client.delete("/api/integrations/justcall")
    assert removed.status_code == 200, removed.text
    assert removed.json()["configured"] is False
    assert DEFAULT_ORG_ID not in store
    after = client.get("/api/integrations/justcall")
    assert after.json()["configured"] is False
    assert after.json().get("key_suffix") in (None, "")


def test_post_api_keys_rejects_justcall(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    key = _jc_key("abcd")
    secret = _jc_secret("efgh")
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post(
        "/api/keys",
        json={"justcall_api_key": key, "justcall_api_secret": secret},
    )
    assert r.status_code == 400
    assert key not in r.text
    assert secret not in r.text
    assert "integrations/justcall" in r.json()["detail"]


def test_save_requires_both_parts(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    _stub_vault(monkeypatch)
    client = TestClient(app)
    authorize(client, monkeypatch)
    r = client.post("/api/integrations/justcall", json={"api_key": _jc_key("only")})
    assert r.status_code == 400
    r = client.post("/api/integrations/justcall/sync")
    assert r.status_code == 503


def test_save_uses_jwt_org_not_body(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    store = _stub_vault(monkeypatch)
    key = _jc_key("orgA")
    secret = _jc_secret("orgA")
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=DEFAULT_ORG_ID)
    r = client.post(
        "/api/integrations/justcall",
        json={
            "api_key": key,
            "api_secret": secret,
            "org_id": ORG_B,
        },
    )
    assert r.status_code == 200, r.text
    assert DEFAULT_ORG_ID in store
    assert ORG_B not in store


def test_org_a_cannot_read_org_b_justcall_status(monkeypatch):
    from backend.api import app
    from tests.conftest import authorize

    store = _stub_vault(monkeypatch)
    store[ORG_B] = {
        "api_key": _jc_key("bbbb"),
        "api_secret": _jc_secret("bbbb"),
        "suffix": "bbbb",
    }
    store[DEFAULT_ORG_ID] = {
        "api_key": _jc_key("aaaa"),
        "api_secret": _jc_secret("aaaa"),
        "suffix": "aaaa",
    }
    client = TestClient(app)
    authorize(client, monkeypatch, org_id=DEFAULT_ORG_ID)
    r = client.get("/api/integrations/justcall")
    assert r.status_code == 200
    assert r.json()["key_suffix"] == "aaaa"
    assert "bbbb" not in r.text
    assert _jc_key("bbbb") not in r.text

    authorize(client, monkeypatch, org_id=ORG_B)
    r = client.get("/api/integrations/justcall")
    assert r.status_code == 200
    assert r.json()["key_suffix"] == "bbbb"
    assert _jc_secret("aaaa") not in r.text


def test_vault_unavailable_returns_503(monkeypatch):
    from backend.api import app
    from backend.org_vault import VaultUnavailable
    from tests.conftest import authorize

    def boom(*_a, **_k):
        raise VaultUnavailable("vault_unavailable")

    monkeypatch.setattr("backend.org_vault.put_justcall", boom)
    client = TestClient(app)
    authorize(client, monkeypatch)
    key = _jc_key("fail")
    secret = _jc_secret("fail")
    r = client.post(
        "/api/integrations/justcall",
        json={"api_key": key, "api_secret": secret},
    )
    assert r.status_code == 503
    assert key not in r.text
    assert secret not in r.text


def test_bound_credentials_do_not_mutate_host_env(monkeypatch):
    from backend import justcall

    monkeypatch.delenv("JUSTCALL_API_KEY", raising=False)
    monkeypatch.delenv("JUSTCALL_API_SECRET", raising=False)
    key = _jc_key("bind")
    secret = _jc_secret("bind")
    with justcall.bound_credentials(key, secret):
        assert justcall.api_key() == key
        assert justcall.api_secret() == secret
        assert not (os.getenv("JUSTCALL_API_KEY") or "").strip()
        assert not (os.getenv("JUSTCALL_API_SECRET") or "").strip()
    assert justcall.api_key() == ""
    assert not justcall.host_configured()
