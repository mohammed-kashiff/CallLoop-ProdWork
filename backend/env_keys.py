"""Upsert allowlisted keys in the repo-root .env. Never log secret values."""

from __future__ import annotations

import os

ALLOWED_ENV_KEYS = frozenset({
    "PYAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "JUSTCALL_API_KEY",
    "JUSTCALL_API_SECRET",
})


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


def upsert_env_value(path: str, name: str, value: str, *, overwrite: bool = True) -> str:
    """
    Write NAME=value into an env file.
    Returns written | kept | created. Does not log `value`.
    """
    if name not in ALLOWED_ENV_KEYS:
        raise ValueError("Unsupported env key.")
    if "\n" in value or "\r" in value or "=" in name:
        raise ValueError("Invalid env value.")

    lines: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    new_lines: list[str] = []
    replaced = False
    outcome = "created"
    prefix = f"{name}="
    for line in lines:
        if line.startswith(prefix):
            existing = line.split("=", 1)[1].strip().strip('"').strip("'")
            if existing and not overwrite:
                new_lines.append(line)
                replaced = True
                outcome = "kept"
            else:
                new_lines.append(f"{prefix}{value}\n")
                replaced = True
                outcome = "written"
        else:
            new_lines.append(line)

    if not replaced:
        if new_lines and not new_lines[-1].endswith("\n"):
            new_lines[-1] = new_lines[-1] + "\n"
        new_lines.append(f"{prefix}{value}\n")
        outcome = "created"

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    return outcome
