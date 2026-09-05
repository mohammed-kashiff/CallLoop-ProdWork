"""Ticket Audit Engine (TA-5): pull embedded raster images out of a ticket
PDF and describe each one with a single Claude vision call.

Standalone and dormant by design — the real JustCall/Intercom PDF export
sample has zero embedded image objects (screenshots collapse to a literal
"[Image]" text token), so this module has nothing to plug into until
ingestion (TA-4) writes ticket_messages. It exists to prove the mechanism
works, validated against a synthetic PDF, so TA-5 isn't blocked on finding
a real image-bearing source first.

Deliberately does not import the call-scoring engine's modules — the call
engine has no equivalent need for PDF image extraction, so this stays its
own small path rather than a shared primitive.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import time

import pypdfium2 as pdfium

from . import applog
from . import pyai_usage

log = logging.getLogger("callproof.ticket_image_extraction")

ANTHROPIC_API_KEY = (os.getenv("ANTHROPIC_API_KEY") or "").strip() or None
MODEL = "claude-sonnet-5"
MAX_HTTP_RETRIES = 3
DESCRIBE_PROMPT = (
    "This image was embedded in a support ticket export (a screenshot a "
    "customer or agent attached). Describe what it shows in 2-4 sentences: "
    "the kind of screen (error message, product UI, order confirmation, "
    "etc.), any visible error text or key values, and anything a support "
    "auditor reviewing this ticket would need to know from it."
)


def extract_images(pdf_bytes: bytes) -> list[dict]:
    """Every embedded raster image in the PDF, in document order.

    Each item: {page_index, image_index, width, height, png_bytes}.
    Returns [] for a PDF with no embedded image objects (the common case
    for the real JustCall export format).
    """
    out: list[dict] = []
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        for page_index in range(len(pdf)):
            page = pdf[page_index]
            image_index = 0
            for obj in page.get_objects():
                if obj.type != pdfium.raw.FPDF_PAGEOBJ_IMAGE:
                    continue
                pil_image = obj.get_bitmap().to_pil().convert("RGB")
                buf = io.BytesIO()
                pil_image.save(buf, format="PNG")
                out.append({
                    "page_index": page_index,
                    "image_index": image_index,
                    "width": pil_image.width,
                    "height": pil_image.height,
                    "png_bytes": buf.getvalue(),
                })
                image_index += 1
    finally:
        pdf.close()
    return out


def describe_image(png_bytes: bytes, *, model=None, timeout=60) -> str:
    """One Claude vision call describing a single embedded image.
    Retries with backoff on 429/5xx, same call_claude() contract used
    elsewhere in this codebase: raises RuntimeError only if every attempt
    fails."""
    if not ANTHROPIC_API_KEY:
        applog.event(
            log, "ticket_image_describe_failure", level=logging.ERROR,
            error="ANTHROPIC_API_KEY is not set",
        )
        raise RuntimeError("ANTHROPIC_API_KEY is not set")
    b64 = base64.b64encode(png_bytes).decode("ascii")
    body = {
        "model": model or MODEL,
        "max_tokens": 500,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": b64},
                },
                {"type": "text", "text": DESCRIBE_PROMPT},
            ],
        }],
    }
    last_err = None
    started = time.perf_counter()
    for attempt in range(1, MAX_HTTP_RETRIES + 1):
        try:
            resp = pyai_usage.post(
                "https://api.anthropic.com/v1/messages",
                provider="anthropic",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json=body,
                timeout=timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                text = "".join(b.get("text", "") for b in data.get("content", [])
                               if b.get("type") == "text")
                applog.event(
                    log, "ticket_image_describe_success",
                    attempt=attempt,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    chars=len(text),
                )
                return text
            last_err = f"{resp.status_code}: {resp.text[:300]}"
            log.warning("ticket image describe attempt %d/%d -> %s", attempt, MAX_HTTP_RETRIES, last_err)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 * attempt)
                continue
            break
        except Exception as e:  # noqa: BLE001
            last_err = applog.safe_exception_text(e)
            log.warning("ticket image describe attempt %d/%d exception: %s", attempt, MAX_HTTP_RETRIES, last_err)
            time.sleep(1)
    applog.event(
        log, "ticket_image_describe_failure", level=logging.ERROR,
        attempts=MAX_HTTP_RETRIES,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        error=last_err,
    )
    raise RuntimeError(f"Claude vision call failed after {MAX_HTTP_RETRIES} attempts: {last_err}")


def extract_and_describe(pdf_bytes: bytes) -> list[dict]:
    """extract_images() + one describe_image() call per image.
    Each item: {page_index, image_index, width, height, description}."""
    out = []
    for img in extract_images(pdf_bytes):
        description = describe_image(img["png_bytes"])
        out.append({
            "page_index": img["page_index"],
            "image_index": img["image_index"],
            "width": img["width"],
            "height": img["height"],
            "description": description,
        })
    return out
