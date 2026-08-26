"""Per-org JustCall credentials in Supabase Vault.

Secrets live in vault.secrets (encrypted). public.org_credentials stores only
org_id, provider, and a key suffix — never the API key or secret.
Read/write always uses a name derived from the JWT/poller org_id
(justcall/{org_id}). Never log decrypted values.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass

from . import db
from .org_ids import bound_org_id, parse_org_id

log = logging.getLogger("callproof.org_vault")

PROVIDER = "justcall"
_lock = threading.Lock()


class VaultUnavailable(Exception):
    """Vault schema/functions are not available on this database."""


class VaultError(Exception):
    """Vault rejected the operation. Message must not include secrets."""


@dataclass(frozen=True)
class JustCallSecret:
    api_key: str
    api_secret: str

    @property
    def suffix(self) -> str | None:
        key = self.api_key
        return key[-4:] if len(key) >= 8 else None


def secret_name(org_id: str) -> str:
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    return f"justcall/{oid}"


def _assert_org_context(oid: str) -> None:
    """When a JWT/worker org is bound, Vault ops must use that org only."""
    bound = bound_org_id()
    if bound and bound != oid:
        raise ValueError("org_mismatch")


def put_justcall(org_id: str, api_key: str, api_secret: str) -> str:
    """Encrypt and store this org's JustCall pair. Returns key suffix."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    _assert_org_context(oid)
    key = (api_key or "").strip()
    secret = (api_secret or "").strip()
    if not key or not secret:
        raise ValueError("missing credentials")
    payload = json.dumps(
        {"api_key": key, "api_secret": secret},
        separators=(",", ":"),
    )
    name = secret_name(oid)
    with _lock:
        try:
            with db.connection(bypass_rls=True) as conn:
                _require_vault(conn)
                existing = conn.execute(
                    "SELECT id FROM vault.secrets WHERE name = %s",
                    (name,),
                ).fetchone()
                if existing:
                    conn.execute(
                        "SELECT vault.update_secret(%s, %s)",
                        (existing["id"], payload),
                    )
                else:
                    conn.execute(
                        "SELECT vault.create_secret(%s, %s, %s)",
                        (payload, name, "per-org justcall"),
                    )
        except VaultUnavailable:
            raise
        except Exception:
            log.info("vault put failed provider=%s org_id=%s", PROVIDER, oid)
            raise VaultError("vault_write_failed") from None
        suffix = key[-4:] if len(key) >= 8 else None
        with db.connection() as conn:
            conn.execute(
                """
                INSERT INTO org_credentials (org_id, provider, key_suffix, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (org_id, provider) DO UPDATE
                SET key_suffix = EXCLUDED.key_suffix,
                    updated_at = now()
                """,
                (oid, PROVIDER, suffix),
            )
    log.info("vault put provider=%s org_id=%s", PROVIDER, oid)
    return suffix or ""


def load_justcall(org_id: str) -> JustCallSecret | None:
    """Decrypt this org's pair. Caller must not log the return value."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    _assert_org_context(oid)
    name = secret_name(oid)
    with db.connection(bypass_rls=True) as conn:
        if not _vault_present(conn):
            return None
        row = conn.execute(
            "SELECT decrypted_secret FROM vault.decrypted_secrets WHERE name = %s",
            (name,),
        ).fetchone()
    if not row:
        return None
    raw = row.get("decrypted_secret")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        raise VaultError("invalid_secret") from None
    key = str(data.get("api_key") or "").strip()
    secret = str(data.get("api_secret") or "").strip()
    if not key or not secret:
        return None
    return JustCallSecret(api_key=key, api_secret=secret)


def delete_justcall(org_id: str) -> bool:
    """Remove this org's Vault secret and index row. Returns True if a row existed."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    _assert_org_context(oid)
    name = secret_name(oid)
    with db.connection() as conn:
        cur = conn.execute(
            "DELETE FROM org_credentials WHERE org_id = %s AND provider = %s",
            (oid, PROVIDER),
        )
        existed = cur.rowcount > 0
    with db.connection(bypass_rls=True) as conn:
        if _vault_present(conn):
            conn.execute("DELETE FROM vault.secrets WHERE name = %s", (name,))
    log.info("vault delete provider=%s org_id=%s", PROVIDER, oid)
    return existed


def status(org_id: str) -> dict:
    """Configured flag + suffix. Never includes the secret."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    _assert_org_context(oid)
    with db.connection() as conn:
        row = conn.execute(
            """
            SELECT key_suffix FROM org_credentials
            WHERE org_id = %s AND provider = %s
            """,
            (oid, PROVIDER),
        ).fetchone()
    if not row:
        return {"configured": False, "suffix": None}
    suffix = row.get("key_suffix")
    return {
        "configured": True,
        "suffix": suffix if isinstance(suffix, str) and suffix else None,
    }


def list_org_ids() -> list[str]:
    """Org ids that have JustCall in Vault. Poller only; no secrets."""
    with db.connection(bypass_rls=True) as conn:
        rows = conn.execute(
            "SELECT org_id FROM org_credentials WHERE provider = %s",
            (PROVIDER,),
        ).fetchall()
    out: list[str] = []
    for row in rows:
        oid = parse_org_id(row["org_id"])
        if oid:
            out.append(oid)
    return out


def _vault_present(conn) -> bool:
    row = conn.execute(
        "SELECT to_regclass('vault.secrets') IS NOT NULL AS ok",
    ).fetchone()
    return bool(row and row["ok"])


def _require_vault(conn) -> None:
    if not _vault_present(conn):
        raise VaultUnavailable("vault_unavailable")
