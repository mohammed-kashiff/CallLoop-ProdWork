"""
CallProof - Intercom OAuth client (IN-3).

OAuth is required here, not a bare access token, because CallLoop connects
to other people's Intercom workspaces, not its own — confirmed directly by
Intercom support: "Apps that access other people's Intercom data must use
OAuth. Asking users for their API access tokens directly is not allowed."

The `state` param carries the initiating org_id, HMAC-signed with the app's
own client_secret (a server-only value already) so the callback can recover
which org started the flow and reject anything tampered with or expired,
with no server-side session/state table needed. The callback is a top-level
browser redirect from a third-party site, not an XHR from CallLoop's own
SPA — it arrives with no usable CallLoop JWT, which is exactly why the org
identity has to travel inside `state` instead (see auth._PUBLIC_PATHS).

Only the OAuth handshake lives here — fetching/parsing conversations and
tickets is separate, later work. Keeping this module to auth only is what
lets it stay a clean boundary the rest of the integration builds behind.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import httpx

from . import applog
from .config import load_env

load_env()
applog.setup_logging()

log = logging.getLogger("callproof.intercom")

AUTHORIZE_URL = "https://app.intercom.com/oauth"
TOKEN_URL = "https://api.intercom.io/auth/eagle/token"
ME_URL = "https://api.intercom.io/me"
PROVIDER = "intercom"

# Long enough for a real login+consent click-through, short enough that a
# leaked/logged authorize URL is useless shortly after.
_STATE_TTL_SECONDS = 600


class IntercomAuthError(Exception):
    """OAuth is not configured, or the token exchange failed. Message must
    never include a client secret, code, or access token."""


class StateError(Exception):
    """The `state` param is missing, malformed, expired, or fails signature
    verification — reject the callback outright, never attempt the token
    exchange on an unverified state."""


def client_id() -> str:
    return (os.getenv("INTERCOM_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.getenv("INTERCOM_CLIENT_SECRET") or "").strip()


def is_configured() -> bool:
    return bool(client_id() and client_secret())


def poll_seconds() -> int:
    """Backstop poll interval (IN-6) — deliberately much lower-frequency
    than JustCall's (45s default): this only needs to catch what a missed
    webhook dropped, not drive primary ingestion, so there's no reason to
    hammer Intercom's Search API on a tight loop."""
    raw = (os.getenv("INTERCOM_POLL_SECONDS") or "300").strip()
    try:
        return max(30, int(raw))
    except ValueError:
        return 300


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str) -> str:
    secret = client_secret()
    if not secret:
        raise IntercomAuthError("intercom_not_configured")
    digest = hmac.new(
        secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256,
    ).digest()
    return _b64url_encode(digest)


def make_state(org_id: str) -> str:
    """A self-verifying CSRF token: org_id + issue time, HMAC-signed. No
    server-side session/state table needed — verify_state() recovers both
    org_id and freshness purely from this value."""
    body = json.dumps(
        {"org_id": org_id, "ts": int(time.time())}, separators=(",", ":"),
    ).encode("utf-8")
    payload_b64 = _b64url_encode(body)
    return f"{payload_b64}.{_sign(payload_b64)}"


def verify_state(state: str | None) -> str:
    """Returns the org_id this state was issued for. Raises StateError on
    any tamper, malformed value, or expiry — callers must not proceed to
    the token exchange if this raises."""
    if not state or "." not in state:
        raise StateError("malformed_state")
    payload_b64, _, sig = state.partition(".")
    try:
        expected = _sign(payload_b64)
    except IntercomAuthError:
        raise StateError("intercom_not_configured") from None
    if not sig or not hmac.compare_digest(expected, sig):
        raise StateError("bad_signature")
    try:
        body = json.loads(_b64url_decode(payload_b64))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise StateError("malformed_state") from None
    if not isinstance(body, dict):
        raise StateError("malformed_state")
    org_id = str(body.get("org_id") or "").strip()
    ts = body.get("ts")
    if not org_id or not isinstance(ts, int):
        raise StateError("malformed_state")
    if time.time() - ts > _STATE_TTL_SECONDS:
        raise StateError("expired_state")
    return org_id


def build_authorize_url(org_id: str) -> str:
    """The URL to redirect the browser to, to start the OAuth handshake."""
    cid = client_id()
    if not cid:
        raise IntercomAuthError("intercom_not_configured")
    state = make_state(org_id)
    return f"{AUTHORIZE_URL}?client_id={cid}&state={state}"


def exchange_code_for_token(code: str) -> str:
    """POST the authorization code for an access token. Never logs the
    code or the returned token — only provider/status on failure."""
    cid = client_id()
    secret = client_secret()
    if not cid or not secret:
        raise IntercomAuthError("intercom_not_configured")
    if not (code or "").strip():
        raise IntercomAuthError("missing_code")
    try:
        r = httpx.post(
            TOKEN_URL,
            json={"code": code, "client_id": cid, "client_secret": secret},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        log.warning(
            "intercom token exchange request failed: %s",
            applog.safe_exception_text(e),
        )
        raise IntercomAuthError("token_exchange_failed") from None
    if r.status_code != 200:
        log.warning("intercom token exchange rejected: status=%s", r.status_code)
        raise IntercomAuthError("token_exchange_failed")
    try:
        body = r.json() if r.content else {}
    except ValueError:
        raise IntercomAuthError("token_exchange_failed") from None
    token = str(body.get("access_token") or "").strip()
    if not token:
        raise IntercomAuthError("token_exchange_failed")
    return token


def fetch_workspace_id(access_token: str) -> str:
    """GET /me with the freshly-exchanged token to learn which workspace we
    just connected — Intercom's `app.id_code`. This is the same value that
    shows up as `app_id` on every webhook payload from that workspace, so
    it's what lets a shared, per-app webhook URL route an inbound event
    back to the right CallLoop org (see org_vault.find_org_id_by_external_account).
    Never logs the token."""
    if not (access_token or "").strip():
        raise IntercomAuthError("missing_code")
    try:
        r = httpx.get(
            ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=30.0,
        )
    except httpx.HTTPError as e:
        log.warning("intercom /me request failed: %s", applog.safe_exception_text(e))
        raise IntercomAuthError("workspace_lookup_failed") from None
    if r.status_code != 200:
        log.warning("intercom /me rejected: status=%s", r.status_code)
        raise IntercomAuthError("workspace_lookup_failed")
    try:
        body = r.json() if r.content else {}
    except ValueError:
        raise IntercomAuthError("workspace_lookup_failed") from None
    app = body.get("app") if isinstance(body, dict) else None
    workspace_id = str((app or {}).get("id_code") or "").strip()
    if not workspace_id:
        raise IntercomAuthError("workspace_lookup_failed")
    return workspace_id


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    """Intercom signs webhook deliveries with `X-Hub-Signature: sha1=<hex>`,
    HMAC-SHA1 over the raw request body, keyed by the app's client_secret
    (Intercom's documented webhook security scheme — same family as
    JustCall's HMAC check, different digest). If the app isn't configured
    at all there's no secret to check against; that state already 503s
    everywhere else Intercom-related, so this fails closed (returns False)
    rather than waving every request through."""
    secret = client_secret()
    if not secret:
        return False
    if not signature:
        return False
    sig = signature.strip()
    if sig.lower().startswith("sha1="):
        sig = sig[5:]
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha1).hexdigest()
    try:
        return hmac.compare_digest(digest, sig)
    except (TypeError, ValueError):
        return False
