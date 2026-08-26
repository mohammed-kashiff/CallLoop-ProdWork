"""
CallProof - stakeholder email helpers.

Primary UX: open a Gmail compose tab with a prefilled churn alert.
Optional: SMTP send via env vars (legacy / automated delivery).

Never log passwords.
"""

from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import urlencode

log = logging.getLogger("callproof.email")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Keep Gmail compose URLs under common browser limits.
_MAX_BODY_CHARS = 1500


def _truthy(name: str, default: str = "false") -> bool:
    return (os.getenv(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def smtp_config():
    host = (os.getenv("SMTP_HOST") or "").strip()
    port_raw = (os.getenv("SMTP_PORT") or "587").strip()
    try:
        port = int(port_raw)
    except ValueError:
        port = 587
    user = (os.getenv("SMTP_USER") or "").strip()
    password = (os.getenv("SMTP_PASSWORD") or "").strip()
    from_addr = (os.getenv("SMTP_FROM") or user).strip()
    to_addr = (os.getenv("STAKEHOLDER_EMAIL") or "").strip()
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "to_addr": to_addr,
        "use_tls": _truthy("SMTP_USE_TLS", "true"),
        "use_ssl": _truthy("SMTP_USE_SSL", "false"),
    }


def is_configured() -> bool:
    c = smtp_config()
    return bool(c["host"] and c["from_addr"] and c["to_addr"] and _EMAIL_RE.match(c["to_addr"]))


def _valid_email(addr: str) -> bool:
    return bool(addr and _EMAIL_RE.match(addr))


def churn_alert_content(call_id: int, audit: dict) -> dict:
    """
    Return {to, subject, body, risk} for a churn/retention stakeholder alert.

    Prefers a Claude retention_email draft when present (drafted on demand at
    compose time); otherwise falls back to a structured template from churn fields.
    """
    churn = audit.get("churn") or {}
    risk = (churn.get("risk") or "unknown").upper()
    score = audit.get("score")
    grade = audit.get("grade") or "—"
    reasoning = (churn.get("reasoning") or "").strip()
    evidence = (churn.get("evidence_text") or "").strip()
    rubric = audit.get("rubric") or "—"
    to_addr = (os.getenv("STAKEHOLDER_EMAIL") or "").strip()
    retention = audit.get("retention_email") or {}

    subject = f"[CallProof] Churn risk {risk} — Call #{call_id}"
    body = ""

    if retention.get("status") == "ok" and (retention.get("body") or "").strip():
        subject = (retention.get("subject") or subject).strip() or subject
        # Prefix with call metadata so the stakeholder has context.
        header = [
            f"Call #{call_id} · Churn risk: {risk} · QA: {score}/100 ({grade})",
            f"Rubric: {rubric}",
            "",
        ]
        body = "\n".join(header) + (retention.get("body") or "").strip()
        actions = retention.get("suggested_actions") or []
        if actions:
            body += "\n\nSuggested actions:\n" + "\n".join(
                f"- {a}" for a in actions if a
            )
        body += "\n\n— Drafted by CallProof from the call transcript"
    else:
        lines = [
            "CallProof churn alert",
            "",
            f"Call ID:      {call_id}",
            f"Churn risk:   {risk}",
            f"QA score:     {score}/100 ({grade})",
            f"Rubric:       {rubric}",
            "",
            "Assessment:",
            reasoning or "(no reasoning provided)",
        ]
        if evidence:
            lines.extend(["", "Customer evidence:", f'"{evidence}"'])
        lines.extend([
            "",
            "Open CallProof to review the full audit and transcript.",
            "",
            "— Drafted by CallProof",
        ])
        body = "\n".join(lines)

    if len(body) > _MAX_BODY_CHARS:
        body = body[: _MAX_BODY_CHARS - 1].rstrip() + "…"

    return {
        "to": to_addr if _valid_email(to_addr) else "",
        "subject": subject,
        "body": body,
        "risk": risk.lower(),
    }


def gmail_compose_url(to: str, subject: str, body: str) -> str:
    """Gmail web compose deep-link (opens compose UI in a new tab when logged in)."""
    params = {
        "view": "cm",
        "fs": "1",
        "tf": "1",
        "su": subject or "",
        "body": body or "",
    }
    if to:
        params["to"] = to
    return "https://mail.google.com/mail/?" + urlencode(params)


def build_compose_payload(call_id: int, audit: dict) -> dict:
    content = churn_alert_content(call_id, audit)
    return {
        **content,
        "gmail_url": gmail_compose_url(
            content["to"], content["subject"], content["body"]
        ),
    }


def build_churn_alert(call_id: int, audit: dict) -> EmailMessage:
    content = churn_alert_content(call_id, audit)
    msg = EmailMessage()
    msg["Subject"] = content["subject"]
    msg.set_content(content["body"])
    return msg


def send_message(msg: EmailMessage, *, to_addr: str | None = None) -> dict:
    """
    Send an EmailMessage via SMTP. Returns a small status dict (no secrets).
    Raises RuntimeError on configuration or delivery failure.
    """
    c = smtp_config()
    if not c["host"]:
        raise RuntimeError(
            "Email is not configured. Set SMTP_HOST, SMTP_FROM (or SMTP_USER), "
            "and STAKEHOLDER_EMAIL on the host."
        )
    dest = (to_addr or c["to_addr"]).strip()
    sender = c["from_addr"]
    if not _valid_email(sender):
        raise RuntimeError("SMTP_FROM / SMTP_USER is missing or not a valid email.")
    if not _valid_email(dest):
        raise RuntimeError("STAKEHOLDER_EMAIL is missing or not a valid email.")

    msg["From"] = sender
    msg["To"] = dest

    log.info(
        "sending email via %s:%s to=%s subject=%r",
        c["host"], c["port"], dest, msg.get("Subject"),
    )
    try:
        if c["use_ssl"]:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(c["host"], c["port"], context=context, timeout=30) as smtp:
                if c["user"]:
                    smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(c["host"], c["port"], timeout=30) as smtp:
                smtp.ehlo()
                if c["use_tls"]:
                    context = ssl.create_default_context()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                if c["user"]:
                    smtp.login(c["user"], c["password"])
                smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        log.error("SMTP auth failed (credentials rejected)")
        raise RuntimeError(
            "SMTP authentication failed. Check SMTP_USER / SMTP_PASSWORD."
        ) from e
    except Exception as e:  # noqa: BLE001
        log.error("SMTP send failed: %s: %s", type(e).__name__, e)
        raise RuntimeError(f"Failed to send email: {type(e).__name__}: {e}") from e

    log.info("email sent to %s", dest)
    return {"status": "sent", "to": dest, "subject": msg.get("Subject")}


def send_churn_alert(call_id: int, audit: dict) -> dict:
    msg = build_churn_alert(call_id, audit)
    return send_message(msg)
