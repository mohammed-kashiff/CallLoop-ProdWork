"""Private per-org call audio in Supabase Storage.

Object key: {org_id}/{call_id}.mp3 — org_id is a UUID (no path traversal).
The bucket is never public. The API uploads and mints signed URLs with the
service role after JWT membership is checked. The browser never sees that key.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from .org_ids import parse_org_id

log = logging.getLogger("callproof.audio_store")

DEFAULT_BUCKET = "call-audio"
DEFAULT_SIGNED_TTL = 3600
_MAX_OBJECT_BYTES = 25 * 1024 * 1024

_bucket_ready = False
_bucket_lock = threading.Lock()


class AudioStoreError(Exception):
    """Storage is misconfigured or the provider rejected the request."""


class AudioNotFound(AudioStoreError):
    """No object for this org/call."""


def bucket_name() -> str:
    raw = (os.getenv("AUDIO_STORAGE_BUCKET") or DEFAULT_BUCKET).strip()
    return raw or DEFAULT_BUCKET


def signed_ttl() -> int:
    raw = (os.getenv("AUDIO_SIGNED_URL_TTL") or "").strip()
    try:
        n = int(raw) if raw else DEFAULT_SIGNED_TTL
    except ValueError:
        n = DEFAULT_SIGNED_TTL
    return max(60, min(n, 24 * 3600))


def configured() -> bool:
    return bool(_supabase_url() and _service_role_key())


def object_key(org_id: str, call_id: int) -> str:
    """Return {org_id}/{call_id}.mp3. Rejects non-UUID org ids."""
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    try:
        cid = int(call_id)
    except (TypeError, ValueError):
        raise ValueError("invalid call_id") from None
    if cid < 1:
        raise ValueError("invalid call_id")
    return f"{oid}/{cid}.mp3"


def put_file(org_id: str, call_id: int, path: str) -> str:
    """Upload a local tempfile to the private bucket. Returns the object key."""
    key = object_key(org_id, call_id)
    ensure_bucket()
    size = os.path.getsize(path)
    if size < 1:
        raise AudioStoreError("empty_object")
    if size > _MAX_OBJECT_BYTES:
        raise AudioStoreError("object_too_large")
    url = f"{_api_root()}/object/{bucket_name()}/{key}"
    headers = {
        **_auth_headers(),
        "Content-Type": _content_type_for_path(path),
        "x-upsert": "true",
    }
    with open(path, "rb") as f:
        body = f.read()
    r = httpx.post(url, headers=headers, content=body, timeout=120.0)
    if r.status_code not in (200, 201):
        log.warning(
            "storage upload failed status=%s key=%s", r.status_code, key,
        )
        raise AudioStoreError("upload_failed")
    log.info("storage put key=%s bytes=%s", key, size)
    return key


def object_exists(org_id: str, call_id: int) -> bool:
    key = object_key(org_id, call_id)
    url = f"{_api_root()}/object/{bucket_name()}/{key}"
    headers = {**_auth_headers(), "Range": "bytes=0-0"}
    r = httpx.get(url, headers=headers, timeout=30.0)
    if r.status_code in (200, 206):
        return True
    if r.status_code in (400, 404):
        return False
    log.warning("storage exists check failed status=%s key=%s", r.status_code, key)
    raise AudioStoreError("exists_failed")


def signed_url(org_id: str, call_id: int) -> tuple[str, int]:
    """Mint a time-limited URL. Never logs the URL or token."""
    key = object_key(org_id, call_id)
    if not object_exists(org_id, call_id):
        raise AudioNotFound("missing")
    ttl = signed_ttl()
    url = f"{_api_root()}/object/sign/{bucket_name()}/{key}"
    r = httpx.post(
        url,
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={"expiresIn": ttl},
        timeout=30.0,
    )
    if r.status_code != 200:
        log.warning("storage sign failed status=%s key=%s", r.status_code, key)
        raise AudioStoreError("sign_failed")
    try:
        payload = r.json()
    except ValueError as e:
        raise AudioStoreError("sign_failed") from e
    rel = payload.get("signedURL") or payload.get("signedUrl") or ""
    if not isinstance(rel, str) or not rel.strip():
        raise AudioStoreError("sign_failed")
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
        raise AudioStoreError("public_url_rejected")
    return absolute, ttl


@contextmanager
def download_to_temp(org_id: str, call_id: int) -> Iterator[str]:
    """Download the object to a tempfile; delete it on exit."""
    key = object_key(org_id, call_id)
    url = f"{_api_root()}/object/{bucket_name()}/{key}"
    r = httpx.get(url, headers=_auth_headers(), timeout=120.0)
    if r.status_code == 404:
        raise AudioNotFound("missing")
    if r.status_code != 200:
        log.warning("storage download failed status=%s key=%s", r.status_code, key)
        raise AudioStoreError("download_failed")
    data = r.content
    if not data:
        raise AudioNotFound("missing")
    fd, path = tempfile.mkstemp(prefix="callproof_", suffix=".mp3")
    os.close(fd)
    try:
        with open(path, "wb") as f:
            f.write(data)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)


def remove_objects(org_id: str, call_ids: list[int]) -> int:
    if not call_ids:
        return 0
    keys = [object_key(org_id, cid) for cid in call_ids]
    return _delete_prefixes(keys)


def remove_org_prefix(org_id: str) -> int:
    oid = parse_org_id(org_id)
    if not oid:
        raise ValueError("invalid org_id")
    return _delete_prefixes([f"{oid}/"])


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
                upd = httpx.put(
                    get_url,
                    headers=headers,
                    json={"public": False},
                    timeout=30.0,
                )
                if upd.status_code not in (200, 201):
                    log.warning("storage bucket public=false failed status=%s", upd.status_code)
                    raise AudioStoreError("bucket_update_failed")
        elif got.status_code in (400, 404):
            created = httpx.post(
                f"{_api_root()}/bucket",
                headers=headers,
                json={
                    "id": name,
                    "name": name,
                    "public": False,
                    "file_size_limit": _MAX_OBJECT_BYTES,
                },
                timeout=30.0,
            )
            if created.status_code not in (200, 201):
                # 409 = already exists (race). Confirm it is private.
                if created.status_code != 409:
                    log.warning(
                        "storage bucket create failed status=%s",
                        created.status_code,
                    )
                    raise AudioStoreError("bucket_create_failed")
        else:
            log.warning("storage bucket get failed status=%s", got.status_code)
            raise AudioStoreError("bucket_get_failed")
        _bucket_ready = True


def _supabase_url() -> str:
    return (os.getenv("SUPABASE_URL") or "").strip().rstrip("/")


def _service_role_key() -> str:
    return (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or "").strip()


def _api_root() -> str:
    url = _supabase_url()
    if not url:
        raise AudioStoreError("storage_not_configured")
    return f"{url}/storage/v1"


def _auth_headers() -> dict[str, str]:
    key = _service_role_key()
    if not key:
        raise AudioStoreError("storage_not_configured")
    return {
        "Authorization": f"Bearer {key}",
        "apikey": key,
    }


def _delete_prefixes(prefixes: list[str]) -> int:
    if not prefixes:
        return 0
    url = f"{_api_root()}/object/{bucket_name()}"
    r = httpx.request(
        "DELETE",
        url,
        headers={**_auth_headers(), "Content-Type": "application/json"},
        json={"prefixes": prefixes},
        timeout=60.0,
    )
    if r.status_code not in (200, 201):
        log.warning("storage delete failed status=%s count=%s", r.status_code, len(prefixes))
        raise AudioStoreError("delete_failed")
    return len(prefixes)


def _content_type_for_path(path: str) -> str:
    try:
        with open(path, "rb") as f:
            head = f.read(12)
    except OSError:
        return "audio/mpeg"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "audio/wav"
    return "audio/mpeg"
