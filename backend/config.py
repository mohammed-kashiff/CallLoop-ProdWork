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

# AC-12 admin console origin. Allowed only when listed in CORS_ORIGINS (see
# render.yaml). Never implied as a wildcard. Keep in sync with frontend
# ADMIN_ORIGIN in frontend/src/lib/adminHost.ts.
ADMIN_ORIGIN = "https://commandcenter.call-loop.com"

# The customer-facing app's fixed origin — API and frontend are separate
# Render services on separate hosts (see ARCHITECTURE.md §2), so any
# server-side redirect meant for the browser-driven customer app (e.g. an
# OAuth callback landing page) must target this explicitly, never a
# relative path or anything derived from the current request. Keep in
# sync with frontend CUSTOMER_ORIGIN in frontend/src/lib/adminHost.ts.
CUSTOMER_ORIGIN = "https://call-loop.com"


def load_env() -> str:
    """Load a gitignored local host env file if present. Safe to call more than once."""
    load_dotenv(ENV_FILE)
    return ENV_FILE


def skip_startup() -> bool:
    """Tests set CALLPROOF_SKIP_STARTUP=1 to import the app without provider bootstrap."""
    return (os.getenv("CALLPROOF_SKIP_STARTUP") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def cors_origins() -> list[str]:
    """Browser origins allowed to call the API. Override with CORS_ORIGINS (comma-separated).

    `*` and `null` are dropped — origin isolation is an explicit allowlist (AC-12).
    """
    raw = (os.getenv("CORS_ORIGINS") or "").strip()
    if not raw:
        candidates = list(_DEFAULT_CORS)
    else:
        candidates = [part.strip() for part in raw.split(",") if part.strip()]
        if not candidates:
            candidates = list(_DEFAULT_CORS)
    out: list[str] = []
    seen: set[str] = set()
    for origin in candidates:
        if origin == "*" or origin.lower() == "null":
            continue
        if origin in seen:
            continue
        seen.add(origin)
        out.append(origin)
    return out or list(_DEFAULT_CORS)
