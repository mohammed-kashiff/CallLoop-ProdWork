"""Compatibility shim. Prefer: uvicorn backend.api:app --reload --port 8000"""

from backend.api import app

__all__ = ["app"]
