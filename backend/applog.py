"""
CallProof persistent logging.

All `callproof.*` loggers write to logs/callproof.log (rotating) in addition to
the terminal. Use `event()` for structured, greppable operational lines.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler

from .paths import LOG_DIR, LOG_FILE

MAX_BYTES = 5 * 1024 * 1024  # 5 MB
BACKUP_COUNT = 5

_CONFIGURED = False

# AC-48/observability PRD §7: a per-request correlation id, threaded into
# every event() line the same way org_id already flows through contextvars
# elsewhere in this codebase (org_ids.py's bind_org_id/bound_org_id). Set
# once per request by RequestIdMiddleware (api.py); background/webhook/CLI
# work that never binds one just omits the field, same as org_id's default.
_REQUEST_ID: ContextVar[str | None] = ContextVar("callproof_request_id", default=None)


def bound_request_id() -> str | None:
    return _REQUEST_ID.get()


def bind_request_id(request_id: str) -> Token:
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    _REQUEST_ID.reset(token)

# Redact secrets before serving logs to the UI or writing the file.
_SECRET_PATTERNS = [
    re.compile(r"(?i)\b(pyai_live_|pyai_test_)[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b(sk-ant-|sk-)[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)(authorization|x-api-key|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]+"),
    re.compile(r"(?i)(justcall_api_(?:key|secret)|api_secret)\s*[:=]\s*\S+"),
    re.compile(
        r"(?i)(betterstack_source_token|logtail_source_token|source_token)\s*[:=]\s*\S+"
    ),
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
        attach_logtail_handler(parent)
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
    parent.addHandler(fh)

    # AC-47/observability PRD §3.2: the redact filter used to be attached
    # only to the file handler above, so a secret printed to console (via
    # api.py's separate logging.basicConfig() root handler) went out
    # unredacted. _RedactFilter mutates record.msg/args in place and
    # always returns True, so attaching it once on the *logger* — not a
    # specific handler — redacts before the record reaches any handler,
    # including the root logger's console handler that `callproof.*`
    # records propagate to. Covers the file handler above too; no need
    # for a second copy on fh specifically.
    parent.addFilter(_RedactFilter())

    # Keep existing per-module basicConfig console handlers; file is additive.
    _CONFIGURED = True
    parent.info("event=logging_ready path=%s", os.path.abspath(LOG_FILE))
    attach_logtail_handler(parent)
    return os.path.abspath(LOG_FILE)


def source_token() -> str:
    """Better Stack (Logtail) source token. Empty disables the hosted sink."""
    return (
        (os.getenv("BETTERSTACK_SOURCE_TOKEN") or "").strip()
        or (os.getenv("LOGTAIL_SOURCE_TOKEN") or "").strip()
    )


def ingesting_host() -> str:
    return (os.getenv("BETTERSTACK_INGESTING_HOST") or "").strip()


def attach_logtail_handler(logger: logging.Logger | None = None) -> bool:
    """Additive Better Stack handler next to the rotating file. Never raises.

    No-op without a source token. Failures talking to the sink are swallowed
    per emit so a down ingest host cannot break scoring or the request.
    """
    parent = logger or logging.getLogger("callproof")
    token = source_token()
    if not token:
        return False
    if any(isinstance(h, _BetterStackHandler) for h in parent.handlers):
        return True
    try:
        from logtail import LogtailHandler
    except Exception:  # noqa: BLE001
        try:
            event(
                logging.getLogger("callproof.applog"),
                "logtail_unavailable",
                level=logging.WARNING,
                error="handler_not_installed",
            )
        except Exception:  # noqa: BLE001
            pass
        return False
    try:
        kwargs: dict = {"source_token": token}
        host = ingesting_host()
        if host:
            kwargs["host"] = host
        inner = LogtailHandler(**kwargs)
        wrapped = _BetterStackHandler(inner)
        wrapped.setLevel(parent.level)
        parent.addHandler(wrapped)
        event(logging.getLogger("callproof.applog"), "logtail_ready")
        return True
    except Exception:  # noqa: BLE001
        try:
            event(
                logging.getLogger("callproof.applog"),
                "logtail_unavailable",
                level=logging.WARNING,
                error="handler_init_failed",
            )
        except Exception:  # noqa: BLE001
            pass
        return False


class _BetterStackHandler(logging.Handler):
    """Wrap LogtailHandler.emit so a network/sink failure never raises.

    logtail-python already nests the logger name under
    context.runtime.logger_name, but Better Stack's dashboard "service"
    dimension reads a top-level field — nested context isn't groupable.
    Stamping record.service mirrors the logger name (e.g. callproof.api)
    into a plain attribute, which logtail-python's frame builder promotes
    to a top-level field, so subsystem-level grouping in Metrics works.
    """

    def __init__(self, inner: logging.Handler) -> None:
        super().__init__()
        self._inner = inner

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not hasattr(record, "service"):
                record.service = record.name
            self._inner.emit(record)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._inner.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


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
    """Write a structured event line: event=<name> [request_id=<id>] key=value ..."""
    parts = [f"event={name}"]
    request_id = _REQUEST_ID.get()
    if request_id:
        parts.append(f"request_id={request_id}")
    for key in sorted(fields):
        parts.append(f"{key}={_fmt_value(fields[key])}")
    logger.log(level, " ".join(parts))


class RequestIdMiddleware:
    """AC-48: one correlation id per request, bound for the request's whole
    lifetime so every applog.event() call inside it carries the same id —
    matters once two things are being processed concurrently. Runs for
    every request, not just authenticated ones (unlike auth.JwtAuthMiddleware,
    which skips public paths) — registered outermost in api.py so the id is
    bound before CORS/auth even run."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex[:16]
        token = bind_request_id(request_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_request_id(token)


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
