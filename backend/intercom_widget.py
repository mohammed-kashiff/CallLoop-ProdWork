"""
CallProof - Intercom Messenger Security via JWT.

Unrelated to the Intercom OAuth integration (intercom_oauth.py /
intercom_client.py / intercom_ingest.py) — that pulls customers' support
data INTO CallLoop for scoring. This is the opposite direction: CallLoop's
own product embeds an Intercom Messenger widget as ITS OWN support
channel, using CallLoop's own Intercom workspace.

The Messenger widget accepts a plain user_id/email with no verification by
default — anyone with devtools could call Intercom({user_id: "someone
else's id", ...}) in the console and see that person's support history.
Messenger Security closes that.

Intercom migrated their security scheme after this integration's initial
build: the old "Identity Verification" (a raw HMAC-SHA256 hex digest of
just the user id, sent as `user_hash`) is still accepted for backward
compatibility, but Intercom's own dashboard only marks the Messenger as
"securely installed" under the new scheme — "Messenger Security with
JWTs". Confirmed by testing: the old user_hash approach sent real,
correctly-computed hashes and Intercom still reported "Insecurely
installed". Rebuilt against the new scheme instead of assuming the old
one was just a UI lag.

The new scheme signs a real JWT (HS256, Intercom's own sample uses
Node's `jsonwebtoken` the same way) with the workspace's "Unified Secret"
as the key, payload carrying user_id (required) and email (optional) —
sent as `intercom_user_jwt`, not `user_hash`. Same secret env var
(INTERCOM_MESSENGER_SECRET) as before; only the thing it produces changed.
"""

from __future__ import annotations

import os
import time

import jwt

_JWT_TTL_SECONDS = 24 * 60 * 60  # Intercom's own sample uses 1h; this integration
# has no token-refresh loop yet (one boot per login/page-load), so a short
# expiry would just log the widget out mid-session for no security benefit —
# the risk this token guards against is a stolen/replayed token, not a long
# validity window on its own. Revisit if a refresh mechanism gets built.


def messenger_secret() -> str:
    return (os.getenv("INTERCOM_MESSENGER_SECRET") or "").strip()


def is_configured() -> bool:
    return bool(messenger_secret())


def user_jwt(user_id: str, email: str | None = None) -> str:
    """Signed JWT for Intercom Messenger Security (`intercom_user_jwt`).
    HS256, keyed by the workspace's Messenger "Unified Secret" — never
    log the secret or the resulting token."""
    secret = messenger_secret()
    if not secret:
        raise RuntimeError("intercom_messenger_not_configured")
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    now = int(time.time())
    payload: dict = {"user_id": uid, "iat": now, "exp": now + _JWT_TTL_SECONDS}
    if email:
        payload["email"] = email
    return jwt.encode(payload, secret, algorithm="HS256")
