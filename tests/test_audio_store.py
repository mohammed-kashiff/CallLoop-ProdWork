"""Per-org Storage keys, private signed URLs, no local audio/ runtime path."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend import audio_store
from backend.org_ids import DEFAULT_ORG_ID
from backend.paths import ROOT

ORG_A = DEFAULT_ORG_ID
ORG_B = "00000000-0000-4000-8000-000000000002"


def test_object_key_isolates_orgs():
    a = audio_store.object_key(ORG_A, 12)
    b = audio_store.object_key(ORG_B, 12)
    assert a == f"{ORG_A}/12.mp3"
    assert b == f"{ORG_B}/12.mp3"
    assert a.startswith(f"{ORG_A}/")
    assert not a.startswith(f"{ORG_B}/")
    assert a != b


def test_object_key_rejects_path_traversal():
    with pytest.raises(ValueError):
        audio_store.object_key("../etc", 1)
    with pytest.raises(ValueError):
        audio_store.object_key(f"{ORG_A}/../{ORG_B}", 1)
    with pytest.raises(ValueError):
        audio_store.object_key("not-a-uuid", 1)
    with pytest.raises(ValueError):
        audio_store.object_key(ORG_A, 0)
    with pytest.raises(ValueError):
        audio_store.object_key(ORG_A, -1)


def test_runtime_has_no_audio_dir_constant():
    import backend.paths as paths

    assert not hasattr(paths, "AUDIO_DIR")
    for path in (ROOT / "backend").glob("*.py"):
        if path.name == "audio_backfill.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "AUDIO_DIR" not in text, path.name


def test_signed_url_is_not_public(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-not-for-prod")
    audio_store._bucket_ready = True
    try:
        exists = MagicMock(status_code=206)
        signed = MagicMock(status_code=200)
        signed.json.return_value = {
            "signedURL": "/object/sign/call-audio/" + f"{ORG_A}/1.mp3?token=abc",
        }
        public = MagicMock(status_code=200)
        public.json.return_value = {
            "signedURL": "/object/public/call-audio/" + f"{ORG_A}/1.mp3",
        }

        with patch("backend.audio_store.httpx.get", return_value=exists):
            with patch("backend.audio_store.httpx.post", return_value=signed):
                url, ttl = audio_store.signed_url(ORG_A, 1)
        assert "/object/sign/" in url
        assert "/object/public/" not in url
        assert url.startswith("https://test.supabase.co/storage/v1/")
        assert ttl >= 60
        assert "token=abc" in url

        with patch("backend.audio_store.httpx.get", return_value=exists):
            with patch("backend.audio_store.httpx.post", return_value=public):
                with pytest.raises(audio_store.AudioStoreError):
                    audio_store.signed_url(ORG_A, 1)
    finally:
        audio_store._bucket_ready = False


def test_get_audio_returns_signed_json(auth_client, monkeypatch):
    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr("backend.api._conn", lambda: _C())
    monkeypatch.setattr(
        "backend.api.transcribe.get_call",
        lambda *a, **k: {"id": 7},
    )
    signed = (
        f"https://test.supabase.co/storage/v1/object/sign/call-audio/{ORG_A}/7.mp3?token=t",
        3600,
    )
    monkeypatch.setattr("backend.api.audio_store.signed_url", lambda *a, **k: signed)
    r = auth_client.get("/api/calls/7/audio")
    assert r.status_code == 200
    body = r.json()
    assert body["url"] == signed[0]
    assert body["expires_in"] == 3600
    assert "/object/sign/" in body["url"]
    assert "/object/public/" not in body["url"]


def test_get_audio_404_does_not_sign(auth_client, monkeypatch):
    class _C:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("must not mint a URL for a missing call")

    monkeypatch.setattr("backend.api._conn", lambda: _C())
    monkeypatch.setattr("backend.api.transcribe.get_call", lambda *a, **k: None)
    monkeypatch.setattr("backend.api.audio_store.signed_url", boom)
    r = auth_client.get("/api/calls/999/audio")
    assert r.status_code == 404
    assert r.status_code != 403
    assert called == []


def test_player_uses_signed_url_json_not_blob():
    player = (ROOT / "frontend" / "src" / "components" / "TranscriptPlayer.tsx").read_text(
        encoding="utf-8",
    )
    assert "createObjectURL" not in player
    assert "body.url" in player
    mapper = (ROOT / "frontend" / "src" / "lib" / "mapAudit.ts").read_text(encoding="utf-8")
    assert "/api/calls/${callId}/audio" in mapper
