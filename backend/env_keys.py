"""Validate allowlisted key formats. Never log secret values.

App-owned credentials (PyAI, Anthropic, Supabase service role) are host
environment variables. This module does not write .env or the database.
"""

from __future__ import annotations


def key_suffix(value: str | None) -> str | None:
    key = (value or "").strip()
    if len(key) < 8:
        return None
    return key[-4:]


def normalize_pyai_key(raw: str) -> str:
    key = (raw or "").strip()
    if "\n" in key or "\r" in key:
        raise ValueError("Invalid PyAI key.")
    if not (key.startswith("pyai_live_") or key.startswith("pyai_test_")):
        raise ValueError("PyAI keys start with pyai_live_ or pyai_test_.")
    if len(key) < 20:
        raise ValueError("That PyAI key looks too short.")
    return key


def normalize_anthropic_key(raw: str) -> str:
    key = (raw or "").strip()
    if "\n" in key or "\r" in key:
        raise ValueError("Invalid Claude key.")
    if not key.startswith("sk-ant-"):
        raise ValueError("Claude keys start with sk-ant-.")
    if len(key) < 20:
        raise ValueError("That Claude key looks too short.")
    return key


def _normalize_justcall_token(raw: str, *, label: str) -> str:
    token = (raw or "").strip()
    if "\n" in token or "\r" in token:
        raise ValueError(f"Invalid JustCall {label}.")
    if any(ch.isspace() for ch in token):
        raise ValueError(f"JustCall {label} must not contain spaces.")
    if ":" in token:
        raise ValueError(f"JustCall {label} is invalid.")
    if len(token) < 8 or len(token) > 256:
        raise ValueError(f"JustCall {label} looks the wrong length.")
    return token


def normalize_justcall_key(raw: str) -> str:
    return _normalize_justcall_token(raw, label="API key")


def normalize_justcall_secret(raw: str) -> str:
    return _normalize_justcall_token(raw, label="API secret")


def pyai_kind(key: str) -> str:
    return "sandbox" if (key or "").startswith("pyai_test_") else "live"
