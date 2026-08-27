import os
import time
import uuid

import jwt
import pytest

os.environ["CALLPROOF_SKIP_STARTUP"] = "1"
os.environ["ERROR_NOTIFY_DESKTOP"] = "false"
os.environ["ERROR_NOTIFY_WEBHOOK_URL"] = ""
os.environ["ERROR_NOTIFY_EMAIL"] = "off"
os.environ.pop("SENTRY_DSN", None)

# Test-only HMAC material so minted JWTs match verify_access_token.
# Not a production secret; pytest overwrites process env for this suite.
TEST_SUPABASE_URL = "https://test.supabase.co"
TEST_JWT_SECRET = "test-only-hs256-not-a-production-secret"
os.environ["SUPABASE_URL"] = TEST_SUPABASE_URL
os.environ["SUPABASE_JWT_SECRET"] = TEST_JWT_SECRET


def mint_access_token(
    *,
    sub: str | None = None,
    exp_delta: int = 3600,
    aud: str = "authenticated",
    secret: str | None = None,
    email: str = "tester@example.com",
) -> str:
    now = int(time.time())
    payload = {
        "sub": sub or str(uuid.uuid4()),
        "aud": aud,
        "iss": f"{TEST_SUPABASE_URL}/auth/v1",
        "exp": now + exp_delta,
        "email": email,
        "role": "authenticated",
    }
    return jwt.encode(payload, secret or TEST_JWT_SECRET, algorithm="HS256")


def authorize(client, monkeypatch, *, sub: str | None = None, org_id: str | None = None) -> str:
    """Attach a valid test JWT and stub org membership. Returns user_id."""
    from backend.auth import Membership
    from backend.org_ids import DEFAULT_ORG_ID

    uid = sub or str(uuid.uuid4())
    tenant = org_id or DEFAULT_ORG_ID
    monkeypatch.setattr(
        "backend.auth.ensure_membership",
        lambda user_id, email=None, first_name=None, last_name=None: Membership(
            tenant, "owner", str(user_id)
        ),
    )
    client.headers["Authorization"] = f"Bearer {mint_access_token(sub=uid)}"
    return uid


@pytest.fixture
def auth_client(monkeypatch):
    from fastapi.testclient import TestClient

    from backend.api import app

    client = TestClient(app)
    authorize(client, monkeypatch)
    return client
