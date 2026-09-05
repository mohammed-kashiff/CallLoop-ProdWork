"""Private per-org ticket screenshots in Supabase Storage (TA-5).

Object key: {org_id}/{ticket_id}/{seq}.png — org_id and ticket_id are
UUIDs (no path traversal). The bucket is never public; the API uploads
and mints signed URLs with the service role. Same shape as audio_store.py
for call-audio, trimmed to what TA-5 needs (no download-to-tempfile, no
bulk delete — a ticket's images are deleted by cascading the ticket row,
storage cleanup for that is a separate concern from writing them here).
"""

from __future__ import annotations

import logging
import os
import threading
import uuid

import httpx

from .org_ids import parse_org_id

log = logging.getLogger("callproof.ticket_image_store")

DEFAULT_BUCKET = "ticket-images"
DEFAULT_SIGNED_TTL = 3600

_bucket_ready = False
_bucket_lock = threading.Lock()


class TicketImageStoreError(Exception):
    """Storage is misconfigured or the provider rejected the request."""


def bucket_name() -> str:
    raw = (os.getenv("TICKET_IMAGE_STORAGE_BUCKET") or DEFAULT_BUCKET).strip()
    return raw or DEFAULT_BUCKET


def signed_ttl() -> int:
    raw = (os.getenv("TICKET_IMAGE_SIGNED_URL_TTL") or "").strip()
    try:
        n = int(raw) if raw else DEFAULT_SIGNED_TTL
    except ValueError:
        n = DEFAULT_SIGNED_TTL
    return max(60, min(n, 24 * 3600))


def configured() -> bool:
    return bool(_supabase_url() and _service_role_key())


def object_key(org_id: str, ticket_id: str, seq: int) -> str:
    """Return {org_id}/{ticket_id}/{seq}.png. Rejects a non-UUID org_id."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    try:
        tid = str(uuid.UUID(str(ticket_id or "").strip()))
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid ticket_id") from None
    try:
        s = int(seq)
    except (TypeError, ValueError):
        raise ValueError("invalid seq") from None
    if s < 0:
        raise ValueError("invalid seq")
    return f"{oid}/{tid}/{s}.png"


def put_bytes(org_id: str, ticket_id: str, seq: int, png_bytes: bytes) -> str:
    """Upload PNG bytes to the private bucket. Returns the object key."""
    if not png_bytes:
        raise TicketImageStoreError("empty_object")
    key = object_key(org_id, ticket_id, seq)
    ensure_bucket()
    url = f"{_api_root()}/object/{bucket_name()}/{key}"
    headers = {
        **_auth_headers(),
        "Content-Type": "image/png",
        "x-upsert": "true",
    }
    r = httpx.post(url, headers=headers, content=png_bytes, timeout=60.0)
    if r.status_code not in (200, 201):
        log.warning("ticket image upload failed status=%s key=%s", r.status_code, key)
        raise TicketImageStoreError("upload_failed")
    log.info("ticket image put key=%s bytes=%s", key, len(png_bytes))
    return key


def signed_url(org_id: str, ticket_id: str, seq: int) -> tuple[str, int]:
    """Mint a time-limited URL for one stored screenshot."""
    key = object_key(org_id, ticket_id, seq)
    ttl = signed_ttl()
    url = f"{_api_root()}/object/sign/{bucket_name()}/{key}"
    r = httpx.post(
        url,
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={"expiresIn": ttl},
        timeout=30.0,
    )
    if r.status_code == 400 or r.status_code == 404:
        raise TicketImageStoreError("not_found")
    if r.status_code != 200:
        log.warning("ticket image sign failed status=%s key=%s", r.status_code, key)
        raise TicketImageStoreError("sign_failed")
    try:
        payload = r.json()
    except ValueError as e:
        raise TicketImageStoreError("sign_failed") from e
    rel = payload.get("signedURL") or payload.get("signedUrl") or ""
    if not isinstance(rel, str) or not rel.strip():
        raise TicketImageStoreError("sign_failed")
    rel = rel.strip()
    if rel.startswith("http://") or rel.startswith("https://"):
        absolute = rel
    elif rel.startswith("/storage/v1"):
        absolute = f"{_supabase_url()}{rel}"
    elif rel.startswith("/"):
        absolute = f"{_api_root()}{rel}"
    else:
        absolute = f"{_api_root()}/{rel.lstrip('/')}"
    if "/object/public/" in absolute:
        raise TicketImageStoreError("public_url_rejected")
    return absolute, ttl


def ensure_bucket() -> None:
    """Create the private bucket if missing; force public=false if it exists."""
    global _bucket_ready
    if _bucket_ready:
        return
    with _bucket_lock:
        if _bucket_ready:
            return
        name = bucket_name()
        headers = {**_auth_headers(), "Content-Type": "application/json"}
        get_url = f"{_api_root()}/bucket/{name}"
        got = httpx.get(get_url, headers=_auth_headers(), timeout=30.0)
        if got.status_code == 200:
            body = got.json() if got.content else {}
            if isinstance(body, dict) and body.get("public") is True:
                upd = httpx.put(get_url, headers=headers, json={"public": False}, timeout=30.0)
                if upd.status_code not in (200, 201):
                    log.warning("ticket image bucket public=false failed status=%s", upd.status_code)
                    raise TicketImageStoreError("bucket_update_failed")
        elif got.status_code in (400, 404):
            created = httpx.post(
                f"{_api_root()}/bucket",
                headers=headers,
                json={"id": name, "name": name, "public": False},
                timeout=30.0,
            )
            if created.status_code not in (200, 201) and created.status_code != 409:
                log.warning("ticket image bucket create failed status=%s", created.status_code)
                raise TicketImageStoreError("bucket_create_failed")
        else:
            log.warning("ticket image bucket get failed status=%s", got.status_code)
            raise TicketImageStoreError("bucket_get_failed")
        _bucket_ready = True


def _supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")


def _service_role_key() -> str:
    return (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()


def _api_root() -> str:
    url = _supabase_url()
    if not url:
        raise TicketImageStoreError("storage_not_configured")
    return f"{url}/storage/v1"


def _auth_headers() -> dict[str, str]:
    key = _service_role_key()
    if not key:
        raise TicketImageStoreError("storage_not_configured")
    return {"Authorization": f"Bearer {key}", "apikey": key}
