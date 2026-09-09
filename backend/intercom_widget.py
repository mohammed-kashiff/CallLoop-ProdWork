"""
CallProof - Intercom Messenger identity verification (Messenger Security).

Unrelated to the Intercom OAuth integration (intercom_oauth.py /
intercom_client.py / intercom_ingest.py) — that pulls customers' support
data INTO CallLoop for scoring. This is the opposite direction: CallLoop's
own product embeds an Intercom Messenger widget as ITS OWN support
channel, using CallLoop's own Intercom workspace.

The Messenger widget accepts a plain user_id/email with no verification by
default — anyone with devtools could call Intercom({user_id: "someone
else's id", ...}) in the console and see that person's support history.
Messenger Security closes that: the backend HMAC-signs the logged-in
user's real id with a secret only the server holds (the workspace's
"Unified Secret", Settings -> Security -> Messenger), and the frontend
passes that signature (`user_hash`) alongside the plain fields — Intercom
recomputes the hash on their end and rejects anything that doesn't match.
"""

from __future__ import annotations

import hashlib
import hmac
import os


def messenger_secret() -> str:
    return (os.getenv("INTERCOM_MESSENGER_SECRET") or "").strip()


def is_configured() -> bool:
    return bool(messenger_secret())


def user_hash(user_id: str) -> str:
    """HMAC-SHA256 of the CallLoop user id — the same Supabase auth uuid
    already trusted everywhere else (request.state.user_id) — keyed by
    the workspace's Messenger Security secret. Never log the secret, and
    never use it for anything other than this one HMAC."""
    secret = messenger_secret()
    if not secret:
        raise RuntimeError("intercom_messenger_not_configured")
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id is required")
    return hmac.new(secret.encode("utf-8"), uid.encode("utf-8"), hashlib.sha256).hexdigest()
