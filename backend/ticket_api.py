"""Ticket Audit Engine HTTP surface (TA-9).

`/api/tickets` and `/api/tickets/upload` — a separate upload path from
`/api/upload` / `/api/upload-batch`. Those ingest audio for the call
engine. This one accepts a JustCall ticket PDF, then hands the bytes to
`ticket_ingest.ingest_ticket_pdf()` (TA-4 text turns + TA-5 screenshot
extract/describe/store). Scoring is not triggered here.

org_id comes from the verified JWT only. The original PDF is not logged.
"""

from __future__ import annotations

import logging
import os
import uuid

from fastapi import File, HTTPException, Request, UploadFile

from . import applog
from . import auth
from . import sentry_report
from . import ticket_image_store
from . import ticket_ingest

log = logging.getLogger("callproof.ticket_api")

MAX_TICKET_PDF_BYTES = 25 * 1024 * 1024
_PDF_MAGIC = b"%PDF"


def _safe_pdf_name(name: str | None) -> str:
    raw = (name or "").strip() or "ticket.pdf"
    base = os.path.basename(raw.replace("\\", "/")).strip().lstrip(".")
    if not base:
        base = "ticket.pdf"
    if len(base) > 180:
        root, ext = os.path.splitext(base)
        base = root[: 180 - len(ext)] + ext
    return base


def _parse_ticket_id(ticket_id: str) -> str:
    try:
        return str(uuid.UUID(str(ticket_id or "").strip()))
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid ticket id.") from None


def upload_ticket(request: Request, file: UploadFile = File(...)):
    """Accept a ticket PDF and run TA-4/TA-5 ingestion. Not /api/upload."""
    org_id = auth.org_id_from_request(request)
    filename = _safe_pdf_name(file.filename)
    data = file.file.read()
    size = len(data)
    applog.event(
        log, "ticket_upload_received",
        filename=filename,
        size_bytes=size,
    )
    if not data:
        applog.event(log, "ticket_upload_rejected", filename=filename, error="empty")
        raise HTTPException(status_code=400, detail="The uploaded file was empty.")
    if size > MAX_TICKET_PDF_BYTES:
        applog.event(
            log, "ticket_upload_rejected",
            filename=filename,
            size_bytes=size,
            error="file_too_large",
        )
        raise HTTPException(
            status_code=413,
            detail=(
                f"File too large ({size / (1024 * 1024):.1f} MB). "
                f"Maximum is {MAX_TICKET_PDF_BYTES // (1024 * 1024)} MB."
            ),
        )
    ext = os.path.splitext(filename)[1].lower()
    if ext and ext != ".pdf":
        applog.event(log, "ticket_upload_rejected", filename=filename, error="not_pdf")
        raise HTTPException(status_code=400, detail="Upload a PDF ticket export.")
    if not data.startswith(_PDF_MAGIC):
        applog.event(log, "ticket_upload_rejected", filename=filename, error="not_pdf")
        raise HTTPException(status_code=400, detail="Upload a PDF ticket export.")

    try:
        ticket_id = ticket_ingest.ingest_ticket_pdf(org_id, data, source="pdf_upload")
    except ValueError:
        applog.event(
            log, "ticket_ingest_failed",
            filename=filename,
            error="unrecognized_pdf",
        )
        raise HTTPException(
            status_code=400,
            detail="This PDF is not a JustCall ticket export.",
        ) from None
    except ticket_image_store.TicketImageStoreError:
        applog.event(
            log, "ticket_ingest_failed",
            filename=filename,
            error="storage_unavailable",
        )
        raise HTTPException(
            status_code=503,
            detail="Ticket image storage is unavailable.",
        ) from None
    except RuntimeError as e:
        applog.event(
            log, "ticket_ingest_failed",
            filename=filename,
            error=applog.safe_exception_text(e),
        )
        raise HTTPException(
            status_code=502,
            detail="Ticket screenshot processing failed.",
        ) from None
    except Exception as e:  # noqa: BLE001
        applog.event(
            log, "ticket_ingest_failed",
            level=logging.ERROR,
            filename=filename,
            error=applog.safe_exception_text(e),
        )
        sentry_report.capture_exception(e)
        raise HTTPException(status_code=502, detail="Ticket ingest failed.") from None

    applog.event(
        log, "ticket_ingested",
        ticket_id=ticket_id,
        filename=filename,
        size_bytes=size,
    )
    return {
        "ticket_id": ticket_id,
        "status": "ready",
        "filename": filename,
        "source": "pdf_upload",
    }


def list_tickets(request: Request):
    org_id = auth.org_id_from_request(request)
    return {"tickets": ticket_ingest.list_tickets(org_id)}


def get_ticket(request: Request, ticket_id: str):
    org_id = auth.org_id_from_request(request)
    tid = _parse_ticket_id(ticket_id)
    row = ticket_ingest.get_ticket(tid, org_id)
    if not row:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    return row


def get_ticket_asset(request: Request, ticket_id: str, seq: int):
    """Time-limited URL for one stored screenshot (TA-5)."""
    org_id = auth.org_id_from_request(request)
    tid = _parse_ticket_id(ticket_id)
    if seq < 0:
        raise HTTPException(status_code=400, detail="Invalid asset seq.")
    meta = ticket_ingest.ticket_asset_meta(tid, org_id, seq)
    if not meta:
        raise HTTPException(status_code=404, detail="Asset not found.")
    try:
        url, ttl = ticket_image_store.signed_url(org_id, tid, seq)
    except ticket_image_store.TicketImageStoreError as e:
        code = str(e)
        if code == "not_found":
            raise HTTPException(status_code=404, detail="Asset not found.") from None
        raise HTTPException(
            status_code=503,
            detail="Ticket image storage is unavailable.",
        ) from None
    return {"url": url, "expires_in": ttl, **meta}


def register(app) -> None:
    """Attach ticket routes onto the FastAPI app.

    Uses add_api_route so these show up as first-class app routes (same
    as /api/upload), not a nested included router. More-specific paths
    are registered first so `{ticket_id}` cannot swallow `upload`.
    """
    app.add_api_route("/api/tickets/upload", upload_ticket, methods=["POST"])
    app.add_api_route("/api/tickets", list_tickets, methods=["GET"])
    app.add_api_route(
        "/api/tickets/{ticket_id}/assets/{seq}", get_ticket_asset, methods=["GET"],
    )
    app.add_api_route("/api/tickets/{ticket_id}", get_ticket, methods=["GET"])
