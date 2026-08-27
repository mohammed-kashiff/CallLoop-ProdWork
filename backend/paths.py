"""Repo-root paths. Independent of process cwd."""

from __future__ import annotations

from pathlib import Path

# backend/paths.py → backend/ → repo root
ROOT = Path(__file__).resolve().parent.parent

LOG_DIR = str(ROOT / "logs")
LOG_FILE = str(ROOT / "logs" / "callproof.log")
RUBRIC_PATH = str(ROOT / "rubric.json")
ENV_FILE = str(ROOT / ".env")
ENV_EXAMPLE = str(ROOT / ".env.example")
