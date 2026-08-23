"""Environment loading and process-wide settings. No secrets in source."""

from __future__ import annotations

import os

from dotenv import load_dotenv

from .paths import ENV_FILE

_DEFAULT_CORS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
)


def load_env() -> str:
    """Load the repo-root .env. Safe to call more than once."""
    load_dotenv(ENV_FILE)
    return ENV_FILE


def skip_startup() -> bool:
    """Tests set CALLPROOF_SKIP_STARTUP=1 to import the app without minting keys."""
    return (os.getenv("CALLPROOF_SKIP_STARTUP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def cors_origins() -> list[str]:
    """Browser origins allowed to call the API. Override with CORS_ORIGINS (comma-separated)."""
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if not raw:
        return list(_DEFAULT_CORS)
    origins = [part.strip() for part in raw.split(",") if part.strip()]
    return origins or list(_DEFAULT_CORS)
