"""Ticket Audit Engine (TA-5) spike: extract embedded images from a ticket
PDF and describe each with one Claude vision call.

The real JustCall/Intercom PDF export sample has zero embedded image
objects, so this is validated against a synthetic, throwaway PDF built at
test time (never committed as a binary fixture, never real ticket data)."""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from backend.paths import ROOT


def _synthetic_pdf_with_image() -> bytes:
    """A one-page PDF with a single embedded RGB image, built in memory."""
    img = Image.new("RGB", (300, 150), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([10, 10, 290, 140], outline=(0, 0, 0), width=3)
    d.text((30, 60), "SYNTHETIC TEST IMAGE", fill=(200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, "PDF")
    return buf.getvalue()


def _synthetic_pdf_without_image() -> bytes:
    """A minimal one-page PDF with a text-only content stream and no
    embedded image objects — the same shape as the real JustCall export
    (vector/text, no raster XObjects). Hand-written rather than via
    PIL.save(..., "PDF"), which always rasterizes the whole page as one
    image XObject and so can never produce an image-free PDF."""
    content = b"BT /F1 12 Tf 72 700 Td (No images here) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (i, obj))
    xref_offset = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.write(b"%010d 00000 n \n" % off)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF"
        % (len(objects) + 1, xref_offset)
    )
    return out.getvalue()


def test_extract_images_finds_the_embedded_image():
    from backend import ticket_image_extraction as tie

    out = tie.extract_images(_synthetic_pdf_with_image())
    assert len(out) == 1
    img = out[0]
    assert img["page_index"] == 0
    assert img["image_index"] == 0
    assert img["width"] == 300
    assert img["height"] == 150
    assert isinstance(img["png_bytes"], bytes)
    assert img["png_bytes"][:8] == b"\x89PNG\r\n\x1a\n"  # real PNG magic bytes


def test_extract_images_returns_empty_list_for_a_pdf_with_no_images():
    from backend import ticket_image_extraction as tie

    assert tie.extract_images(_synthetic_pdf_without_image()) == []


def test_describe_image_sends_a_base64_image_content_block(monkeypatch):
    from backend import ticket_image_extraction as tie

    monkeypatch.setattr(tie, "ANTHROPIC_API_KEY", "test-key")
    seen = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"content": [{"type": "text", "text": "A login error screen."}]}

    def _fake_post(url, *, provider, headers, json, timeout):
        seen["url"] = url
        seen["provider"] = provider
        seen["json"] = json
        return _Resp()

    monkeypatch.setattr(tie.pyai_usage, "post", _fake_post)

    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    result = tie.describe_image(png_bytes)

    assert result == "A login error screen."
    assert seen["provider"] == "anthropic"
    content = seen["json"]["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["media_type"] == "image/png"
    import base64
    assert base64.b64decode(image_block["source"]["data"]) == png_bytes
    text_block = next(b for b in content if b["type"] == "text")
    assert "screenshot" in text_block["text"].lower() or "image" in text_block["text"].lower()


def test_describe_image_raises_without_an_api_key(monkeypatch):
    from backend import ticket_image_extraction as tie

    monkeypatch.setattr(tie, "ANTHROPIC_API_KEY", None)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        tie.describe_image(b"irrelevant")


def test_describe_image_retries_on_5xx_then_succeeds(monkeypatch):
    from backend import ticket_image_extraction as tie

    monkeypatch.setattr(tie, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(tie.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    class _Resp:
        def __init__(self, status_code, text="", body=None):
            self.status_code = status_code
            self.text = text
            self._body = body or {}

        def json(self):
            return self._body

    def _flaky_post(*_a, **_k):
        calls["n"] += 1
        if calls["n"] < 2:
            return _Resp(503, text="upstream overloaded")
        return _Resp(200, body={"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(tie.pyai_usage, "post", _flaky_post)
    assert tie.describe_image(b"irrelevant") == "ok"
    assert calls["n"] == 2


def test_describe_image_raises_after_exhausting_retries(monkeypatch):
    from backend import ticket_image_extraction as tie

    monkeypatch.setattr(tie, "ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(tie.time, "sleep", lambda *_: None)

    class _Resp:
        status_code = 500
        text = "server error"

        def json(self):
            return {}

    monkeypatch.setattr(tie.pyai_usage, "post", lambda *_a, **_k: _Resp())
    with pytest.raises(RuntimeError, match="Claude vision call failed"):
        tie.describe_image(b"irrelevant")


def test_extract_and_describe_combines_both_steps(monkeypatch):
    from backend import ticket_image_extraction as tie

    monkeypatch.setattr(tie, "describe_image", lambda png_bytes: "a description")
    out = tie.extract_and_describe(_synthetic_pdf_with_image())
    assert len(out) == 1
    assert out[0]["description"] == "a description"
    assert out[0]["width"] == 300
    assert out[0]["height"] == 150
    assert "png_bytes" not in out[0]


def test_module_does_not_import_the_call_scoring_engine():
    """TA-5 is its own small path, not a shared primitive with the call
    engine (which has no equivalent need for PDF image extraction)."""
    src = (ROOT / "backend" / "ticket_image_extraction.py").read_text(encoding="utf-8")
    assert "qa_engine" not in src
    assert "qa_v8" not in src
    assert "rules_v8" not in src
