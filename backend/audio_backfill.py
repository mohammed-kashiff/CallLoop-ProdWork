"""Upload leftover local recordings into the private Storage bucket.

Usage:
    python -m backend.audio_backfill [directory]

Default directory is ./audio (legacy local copies). This module is the only
place that still accepts that path — runtime API code does not.

Looks up org_id from calls (bypass_rls, Alembic-adjacent) then upserts
{org_id}/{call_id}.mp3. Does not delete local files.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path

from . import audio_store
from . import db
from .config import load_env
from .paths import ROOT

log = logging.getLogger("callproof.audio_backfill")

_CALL_FILE = re.compile(r"^(\d+)\.mp3$", re.IGNORECASE)


def _iter_recordings(directory: Path) -> list[tuple[int, Path]]:
    if not directory.is_dir():
        return []
    found: list[tuple[int, Path]] = []
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        if name.startswith("_"):
            continue
        m = _CALL_FILE.match(name)
        if not m:
            continue
        found.append((int(m.group(1)), entry))
    return found


def _org_for_call(call_id: int) -> str | None:
    with db.connection(bypass_rls=True) as conn:
        row = conn.execute(
            "SELECT org_id FROM calls WHERE id = %s",
            (call_id,),
        ).fetchone()
    if not row:
        return None
    return str(row["org_id"])


def backfill(directory: Path) -> dict[str, int]:
    stats = {"uploaded": 0, "skipped": 0, "missing_call": 0, "errors": 0}
    files = _iter_recordings(directory)
    if not files:
        log.info("no recordings in %s", directory)
        return stats
    audio_store.ensure_bucket()
    for call_id, path in files:
        try:
            org_id = _org_for_call(call_id)
            if not org_id:
                log.warning("no calls row for %s", path.name)
                stats["missing_call"] += 1
                continue
            audio_store.put_file(org_id, call_id, str(path))
            log.info("backfilled call_id=%s org_id=%s", call_id, org_id)
            stats["uploaded"] += 1
        except Exception:
            log.exception("backfill failed for %s", path.name)
            stats["errors"] += 1
    return stats


def main(argv: list[str] | None = None) -> int:
    load_env()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(
        description="Upload leftover local recordings to private Storage.",
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(ROOT / "audio"),
        help="Directory of leftover {call_id}.mp3 files (default: <repo>/audio)",
    )
    args = parser.parse_args(argv)
    directory = Path(args.directory).expanduser().resolve()
    if not audio_store.configured():
        log.error("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        return 2
    if not directory.is_dir():
        log.error("directory does not exist: %s", directory)
        return 2
    stats = backfill(directory)
    log.info(
        "backfill done uploaded=%s missing_call=%s errors=%s skipped=%s",
        stats["uploaded"], stats["missing_call"], stats["errors"], stats["skipped"],
    )
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
