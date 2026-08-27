"""
Notify operators when the CallProof HTTP API fails.

Default: macOS Notification Center (local). Optional webhook and/or email.
Never include secrets, request bodies, or webhook URLs in logs or payloads.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import threading
import time
from email.message import EmailMessage
from typing import Any

import httpx

from . import applog

log = logging.getLogger("callproof.notify")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Operator inboxes for API 5xx alerts. Set ERROR_NOTIFY_EMAIL=off to disable.
DEFAULT_ERROR_NOTIFY_EMAILS = (
    "mohammed.kashif@saaslabs.co",
    "mohammedkashif291@gmail.com",
)

_lock = threading.Lock()
_last_sent: dict[str, float] = {}
_ready_logged = False


def _truthy(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def min_interval_seconds() -> int:
    return max(5, _int_env("ERROR_NOTIFY_MIN_INTERVAL_SECONDS", 60))


def desktop_enabled() -> bool:
    if os.getenv("ERROR_NOTIFY_DESKTOP", "").strip():
        return _truthy("ERROR_NOTIFY_DESKTOP")
    return platform.system() == "Darwin"


def webhook_url() -> str:
    return (os.getenv("ERROR_NOTIFY_WEBHOOK_URL") or "").strip()


def _parse_emails(raw: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in raw.replace(";", ",").split(","):
        addr = part.strip()
        key = addr.lower()
        if addr and _EMAIL_RE.match(addr) and key not in seen:
            seen.add(key)
            found.append(addr)
    return found


def notify_emails() -> list[str]:
    raw = os.getenv("ERROR_NOTIFY_EMAIL")
    if raw is not None and raw.strip().lower() in ("off", "false", "0", "none"):
        return []
    extras = _parse_emails(raw or "")
    found: list[str] = []
    seen: set[str] = set()
    for addr in (*DEFAULT_ERROR_NOTIFY_EMAILS, *extras):
        key = addr.lower()
        if addr and _EMAIL_RE.match(addr) and key not in seen:
            seen.add(key)
            found.append(addr)
    return found


def notify_email() -> str:
    addrs = notify_emails()
    return addrs[0] if addrs else ""


def smtp_available() -> bool:
    from . import email_notify

    cfg = email_notify.smtp_config()
    host = (cfg.get("host") or "").strip()
    from_addr = (cfg.get("from_addr") or "").strip()
    return bool(host and _EMAIL_RE.match(from_addr))


def email_transport() -> str | None:
    """How email is sent: smtp, mail.app, or None. Address-only is enough on macOS Mail."""
    if not notify_emails():
        return None
    if smtp_available():
        return "smtp"
    if platform.system() == "Darwin":
        return "mail.app"
    return None


def should_notify_status(status: int) -> bool:
    """5xx only. 4xx are client/input issues and would spam."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return False
    return code >= 500


def channels_enabled() -> dict[str, bool]:
    return {
        "desktop": desktop_enabled(),
        "webhook": bool(webhook_url()),
        "email": email_transport() is not None,
    }


def log_ready() -> None:
    """Once per process: which channels are on (no URLs, no addresses)."""
    global _ready_logged
    if _ready_logged:
        return
    ch = channels_enabled()
    applog.event(
        log,
        "error_notify_ready",
        desktop=ch["desktop"],
        webhook=ch["webhook"],
        email=ch["email"],
        email_via=email_transport() or "off",
        recipient_count=len(notify_emails()),
        min_interval_s=min_interval_seconds(),
    )
    _ready_logged = True


def _cooldown_key(method: str, path: str, status: int) -> str:
    return f"{method.upper()}|{path}|{status}"


def _in_cooldown(key: str) -> bool:
    now = time.monotonic()
    interval = min_interval_seconds()
    with _lock:
        last = _last_sent.get(key)
        if last is not None and now - last < interval:
            return True
        _last_sent[key] = now
        return False


def _apple_quote(text: str, max_len: int = 180) -> str:
    cleaned = " ".join((text or "").split())[:max_len]
    return '"' + cleaned.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _send_desktop(title: str, subtitle: str, message: str) -> None:
    if platform.system() != "Darwin":
        return
    script = (
        f"display notification {_apple_quote(message)} with title {_apple_quote(title)}"
    )
    if subtitle:
        script += f" subtitle {_apple_quote(subtitle)}"
    subprocess.run(
        ["osascript", "-e", script],
        check=False,
        timeout=5,
        capture_output=True,
    )


def _send_webhook(text: str) -> None:
    url = webhook_url()
    if not url:
        return
    # Slack-compatible; also fine for generic JSON receivers.
    httpx.post(url, json={"text": text}, timeout=5.0)


def _send_email_via_mail_app(to_addrs: list[str], subject: str, body: str) -> None:
    """Send through the signed-in macOS Mail account. No SMTP settings required."""
    recipient_lines = "\n".join(
        "make new to recipient at end of to recipients with properties "
        f"{{address:{_apple_quote(addr, 80)}}}"
        for addr in to_addrs
    )
    script = (
        'tell application "Mail"\n'
        "set newMessage to make new outgoing message with properties "
        f"{{subject:{_apple_quote(subject, 120)}, "
        f"content:{_apple_quote(body, 700)}, visible:false}}\n"
        "tell newMessage\n"
        f"{recipient_lines}\n"
        "send\n"
        "end tell\n"
        "end tell"
    )
    result = subprocess.run(
        ["osascript", "-e", script],
        check=False,
        timeout=20,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Mail.app send failed")


def _send_email(subject: str, body: str) -> None:
    dests = notify_emails()
    transport = email_transport()
    if not dests or not transport:
        return
    if transport == "smtp":
        from . import email_notify

        for dest in dests:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg.set_content(body)
            email_notify.send_message(msg, to_addr=dest)
        return
    _send_email_via_mail_app(dests, subject, body)


def _safe_text(*parts: Any) -> str:
    raw = " ".join(str(p) for p in parts if p is not None and str(p) != "")
    return applog.redact_line(raw)


def notify_http_error(
    *,
    method: str,
    path: str,
    status: int,
    error: str | None = None,
    duration_ms: float | None = None,
) -> None:
    """
    Fire-and-forget operator alert. Never raises to the request.
    No-ops for 4xx and while a matching alert is in cooldown.
    """
    if not should_notify_status(status):
        return
    if not any(channels_enabled().values()):
        return

    key = _cooldown_key(method, path, status)
    if _in_cooldown(key):
        applog.event(
            log,
            "error_notify_suppressed",
            method=method,
            path=path,
            status=status,
        )
        return

    title = "CallProof API error"
    subtitle = _safe_text(f"{method} {path} → {status}")
    err = _safe_text(error) if error else ""
    dur = f" duration_ms={duration_ms}" if duration_ms is not None else ""
    message = _safe_text(f"{subtitle}{(' ' + err) if err else ''}{dur}")
    email_body = "\n".join(
        [
            "CallProof HTTP API error",
            "",
            f"Method:  {method}",
            f"Path:    {path}",
            f"Status:  {status}",
            f"Error:   {err or '—'}",
            f"Duration:{dur.strip() or '—'}",
            "",
            "See logs/callproof.log for the matching event=http_error line.",
            "Secrets are redacted from this notice.",
        ]
    )

    def _run() -> None:
        sent = []
        try:
            if desktop_enabled():
                _send_desktop(title, subtitle, err or subtitle)
                sent.append("desktop")
        except Exception as e:  # noqa: BLE001
            log.warning("desktop notify failed: %s", type(e).__name__)
        try:
            if webhook_url():
                _send_webhook(f"{title}: {message}")
                sent.append("webhook")
        except Exception as e:  # noqa: BLE001
            log.warning("webhook notify failed: %s", type(e).__name__)
        try:
            if email_transport():
                _send_email(f"{title}: {method} {path} ({status})", email_body)
                sent.append("email")
        except Exception as e:  # noqa: BLE001
            log.warning("email notify failed: %s", type(e).__name__)
        applog.event(
            log,
            "error_notify_sent",
            method=method,
            path=path,
            status=status,
            channels=",".join(sent) or "none",
            recipient_count=len(notify_emails()) if "email" in sent else 0,
        )

    threading.Thread(target=_run, name="callproof-error-notify", daemon=True).start()
