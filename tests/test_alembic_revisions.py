"""Guards the exact class of bug that broke the 2026-09-08 deploy: a
migration's `revision` id was 37 characters, alembic_version.version_num is
VARCHAR(32) (Alembic's own default), Postgres truncation-errored on the
version bump, and the whole migration rolled back mid-deploy. Every revision
id in this repo must fit the column it gets written into, full stop.
"""

from __future__ import annotations

import ast
from pathlib import Path

from backend.paths import ROOT

VERSIONS_DIR = ROOT / "alembic" / "versions"
_MAX_REVISION_LEN = 32  # alembic_version.version_num VARCHAR(32)


def _revision_and_down_revision(path: Path) -> tuple[str, object]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    revision = None
    down_revision = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "revision" in targets and isinstance(node.value, ast.Constant):
            revision = node.value.value
        if "down_revision" in targets:
            down_revision = ast.literal_eval(node.value)
    return revision, down_revision


def _all_migrations() -> list[Path]:
    return sorted(VERSIONS_DIR.glob("*.py"))


def test_every_revision_id_fits_the_alembic_version_column():
    too_long = []
    for path in _all_migrations():
        revision, _ = _revision_and_down_revision(path)
        if revision and len(revision) > _MAX_REVISION_LEN:
            too_long.append(f"{path.name}: revision={revision!r} ({len(revision)} chars)")
    assert too_long == [], (
        f"revision id(s) exceed alembic_version.version_num's "
        f"VARCHAR({_MAX_REVISION_LEN}) — the deploy will truncation-error on "
        f"the version bump and roll back the whole migration: " + "; ".join(too_long)
    )


def test_every_down_revision_id_fits_the_alembic_version_column():
    too_long = []
    for path in _all_migrations():
        _, down_revision = _revision_and_down_revision(path)
        candidates = down_revision if isinstance(down_revision, (list, tuple)) else [down_revision]
        for d in candidates:
            if isinstance(d, str) and len(d) > _MAX_REVISION_LEN:
                too_long.append(f"{path.name}: down_revision={d!r} ({len(d)} chars)")
    assert too_long == [], "down_revision id(s) exceed VARCHAR(32): " + "; ".join(too_long)


def test_revision_ids_are_unique():
    seen: dict[str, str] = {}
    dupes = []
    for path in _all_migrations():
        revision, _ = _revision_and_down_revision(path)
        if not revision:
            continue
        if revision in seen:
            dupes.append(f"{revision!r} in both {seen[revision]} and {path.name}")
        seen[revision] = path.name
    assert dupes == [], "duplicate revision id(s): " + "; ".join(dupes)
