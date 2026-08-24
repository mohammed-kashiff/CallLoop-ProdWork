"""
CallProof persistent logging.

All `callproof.*` loggers write to logs/callproof.log (rotating) in addition to
the terminal. Use `event()` for structured, greppable operational lines.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler

from .paths import LOG_DIR, LOG_FILE

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 5

_CONFIGURED = False

# Redact secrets before serving logs to the UI or writing the file.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(pyai_live_|pyai_test_)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(sk-ant-|sk-)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(authorization|x-api-key|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*"),
    re.compile(r"(?i)illegal header value\s+\S+"),
]


def setup_logging(level: int = logging.INFO) -> str:
    """
    Attach a rotating file handler to the callproof logger tree.
    Safe to call more than once. Returns the log file path.
    """
    global _CONFIGURED
    os.makedirs(LOG_DIR, exist_ok=True)

    parent = logging.getLogger("callproof")
    parent.setLevel(level)

    if _CONFIGURED:
        return os.path.abspath(LOG_FILE)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(fmt)
    fh.addFilter(_RedactFilter())
    parent.addHandler(fh)

    # Keep existing per-module basicConfig console handlers; file is additive.
    _CONFIGURED = True
    parent.info("event=logging_ready path=%s", os.path.abspath(LOG_FILE))
    return os.path.abspath(LOG_FILE)


def _fmt_value(value) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    text = str(value).replace("\n", " ").replace("\r", " ").strip()
    text = redact_line(text)
    if " " in text or "=" in text:
        text = text.replace('"', "'")
        return f'"{text}"'
    return text


def event(logger: logging.Logger, name: str, level: int = logging.INFO, **fields):
    """Write a structured event line: event=<name> key=value ..."""
    parts = [f"event={name}"]
    for key in sorted(fields):
        parts.append(f"{key}={_fmt_value(fields[key])}")
    logger.log(level, " ".join(parts))


def redact_line(line: str) -> str:
    """Strip API keys / bearer tokens / illegal-header blobs from a log line."""
    text = line or ""
    for pat in _SECRET_PATTERNS:
        text = pat.sub("[REDACTED]", text)
    return text


def safe_exception_text(exc: BaseException) -> str:
    """Log-safe exception summary. Never includes header values or key material."""
    name = type(exc).__name__
    raw = str(exc) or ""
    lowered = raw.lower()
    if "illegal header" in lowered or "header value" in lowered:
        return f"{name}: illegal HTTP header value"
    redacted = redact_line(raw)
    return f"{name}: {redacted}" if redacted else name


class _RedactFilter(logging.Filter):
    """Last line of defense so a secret in record.msg/args never hits the log file."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_line(record.msg)
        args = record.args
        if isinstance(args, dict):
            record.args = {
                k: redact_line(v) if isinstance(v, str) else v
                for k, v in args.items()
            }
        elif isinstance(args, tuple):
            record.args = tuple(
                redact_line(a) if isinstance(a, str) else a for a in args
            )
        return True


def read_tail(lines: int = 200, path: str | None = None) -> dict:
    """
    Return the last N lines of the CallProof log file (redacted).
    Same content the terminal file logger receives for callproof.* loggers.
    """
    n = max(1, min(int(lines or 200), 2000))
    log_path = os.path.abspath(path or LOG_FILE)
    if not os.path.isfile(log_path):
        return {
            "ok": False,
            "path": log_path,
            "lines": [],
            "count": 0,
            "error": "log_file_missing",
            "message": "No log file yet — make an API request first.",
        }

    try:
        with open(log_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 8192
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                read_size = min(block, size)
                size -= read_size
                f.seek(size)
                data = f.read(read_size) + data
            text = data.decode("utf-8", errors="replace")
    except OSError as e:
        return {
            "ok": False,
            "path": log_path,
            "lines": [],
            "count": 0,
            "error": "read_failed",
            "message": str(e),
        }

    raw_lines = text.splitlines()
    if len(raw_lines) > n:
        raw_lines = raw_lines[-n:]
    safe = [redact_line(ln) for ln in raw_lines]
    return {
        "ok": True,
        "path": log_path,
        "lines": safe,
        "count": len(safe),
        "requested": n,
    }
