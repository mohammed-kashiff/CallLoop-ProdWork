"""Backfill CLI picks {call_id}.mp3 only — skips temps and batch dirs."""

from __future__ import annotations

from pathlib import Path

from backend.audio_backfill import _iter_recordings


def test_iter_recordings_skips_temps_and_non_id_files(tmp_path: Path):
    (tmp_path / "12.mp3").write_bytes(b"x")
    (tmp_path / "3.MP3").write_bytes(b"x")
    (tmp_path / "_upload_abc").write_bytes(b"x")
    (tmp_path / "_justcall_abc.mp3").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    nested = tmp_path / "batches"
    nested.mkdir()
    (nested / "1.mp3").write_bytes(b"x")

    found = _iter_recordings(tmp_path)
    ids = sorted(cid for cid, _ in found)
    assert ids == [3, 12]
