"""TA-4: deterministic parser for the confirmed JustCall ticket-export
template (verified against a real sample in TA-2 — see
ticket_pdf_parser.py's module docstring for the exact format)."""

from __future__ import annotations

import io
import os

import pytest

from backend import ticket_pdf_parser as tpp

SAMPLE_TEXT = """\
Conversation with JustCall
Ticket ID: #125936913
Started on August 19, 2026 at 06:16 AM Asia/Calcutta time IST (GMT+0530)
Ticket with JustCall
Started on August 19, 2026 at 06:16 AM Asia/Calcutta time IST (GMT+0530)
Participants
Kevin Abraham (kevin@example.com)
Mike Mikhaiel (mike@example.com)
Ticket Details
Ticket ID: #125936913
Ticket state category: request
Ticket status: Closed
--- August 19, 2026 ---
06:16 AM | Kevin Abraham: But why did it fail this time
06:17 AM | Welma Bot: Hi Mike,
Your campaign was rejected because of the opt-in information you provided.
Please let me know if this helps.
07:11 AM | Tanu from JustCall: Hello Kevin,
I hope you are doing well. My name is Tanu.
--- August 20, 2026 ---
03:32 PM | Dhruv from JustCall: Hi Kevin,
04:11 PM | Kevin Abraham: Hi Dhruv,
Thanks for the update.
Exported from JustCall on September 5, 2026 at 03:46 AM Asia/Calcutta time IST (GMT+0530)
"""


def test_looks_like_justcall_export_detects_the_known_template():
    assert tpp.looks_like_justcall_export(SAMPLE_TEXT) is True


def test_looks_like_justcall_export_rejects_unrelated_text():
    assert tpp.looks_like_justcall_export("Just some random PDF text.") is False


def test_parse_turns_produces_one_entry_per_turn_in_order():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    assert [t["seq"] for t in turns] == [0, 1, 2, 3, 4]
    assert [t["speaker_name"] for t in turns] == [
        "Kevin Abraham", "Welma Bot", "Tanu", "Dhruv", "Kevin Abraham",
    ]


def test_parse_turns_classifies_agent_bot_and_customer_roles():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    roles = {t["speaker_name"]: t["speaker"] for t in turns}
    assert roles["Kevin Abraham"] == "customer"
    assert roles["Welma Bot"] == "bot"
    assert roles["Tanu"] == "agent"
    assert roles["Dhruv"] == "agent"


def test_parse_turns_strips_the_from_justcall_suffix_from_agent_names():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    agent_turn = next(t for t in turns if t["speaker_name"] == "Tanu")
    assert agent_turn["speaker"] == "agent"
    assert "from JustCall" not in agent_turn["speaker_name"]


def test_parse_turns_joins_wrapped_lines_into_the_same_turn():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    bot_turn = turns[1]
    assert bot_turn["text"] == (
        "Hi Mike,\n"
        "Your campaign was rejected because of the opt-in information you provided.\n"
        "Please let me know if this helps."
    )


def test_parse_turns_skips_header_participants_and_ticket_details():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    all_text = " ".join(t["text"] for t in turns)
    assert "Ticket ID" not in all_text
    assert "Participants" not in all_text
    assert "kevin@example.com" not in all_text


def test_parse_turns_excludes_the_export_footer():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    all_text = " ".join(t["text"] for t in turns)
    assert "Exported from JustCall" not in all_text
    # the footer must not be folded into the last real turn either
    assert turns[-1]["text"] == "Hi Dhruv,\nThanks for the update."


def test_parse_turns_ignores_day_separator_lines():
    turns = tpp.parse_turns(SAMPLE_TEXT)
    for t in turns:
        assert not t["text"].startswith("---")


def test_agent_user_id_is_always_none_for_now():
    """No table in this codebase currently resolves a raw ticket display
    name to a real user id — this is a documented gap, not a bug."""
    turns = tpp.parse_turns(SAMPLE_TEXT)
    assert all(t["agent_user_id"] is None for t in turns)


def test_parse_ticket_pdf_rejects_non_justcall_pdfs(monkeypatch):
    monkeypatch.setattr(tpp, "extract_text", lambda pdf_bytes: "Some unrelated PDF content.")
    with pytest.raises(ValueError, match="JustCall export template"):
        tpp.parse_ticket_pdf(b"irrelevant")


def _build_single_page_pdf(text: str, page_height: int = 1600) -> bytes:
    """Minimal real one-page PDF (hand-written, no external library) whose
    content stream is exactly the given text — used to run real
    PDF-text-extraction (pdfplumber), not just the string-level parser."""
    content = text.replace("(", r"\(").replace(")", r"\)")
    lines = content.split("\n")
    stream_ops = ["BT", "/F1 10 Tf", "72 750 Td", "12 TL"]
    for line in lines:
        stream_ops.append(f"({line}) Tj")
        stream_ops.append("T*")
    stream_ops.append("ET")
    stream = "\n".join(stream_ops).encode("latin-1", errors="replace")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 %d] /Contents 5 0 R >>" % page_height,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
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


def test_parse_ticket_pdf_end_to_end_with_a_real_pdf():
    pdf_bytes = _build_single_page_pdf(SAMPLE_TEXT)
    turns = tpp.parse_ticket_pdf(pdf_bytes)
    assert len(turns) == 5
    assert turns[0]["speaker"] == "customer"
    assert turns[2]["speaker"] == "agent"


def test_parse_turns_with_pages_tags_each_turn_with_its_starting_page(monkeypatch):
    page_0 = "\n".join([
        "--- August 19, 2026 ---",
        "06:16 AM | Kevin Abraham: But why did it fail this time",
        "06:17 AM | Welma Bot: Hi Mike,",
    ])
    page_1 = "\n".join([
        "07:11 AM | Tanu from JustCall: Hello Kevin,",
        "I hope you are doing well.",
        "--- August 20, 2026 ---",
        "03:32 PM | Dhruv from JustCall: Hi Kevin,",
    ])
    monkeypatch.setattr(tpp, "extract_pages_text", lambda pdf_bytes: [page_0, page_1])

    turns = tpp.parse_turns_with_pages(b"irrelevant")
    assert [t["page_index"] for t in turns] == [0, 0, 1, 1]
    assert [t["seq"] for t in turns] == [0, 1, 2, 3]
    # a message that wraps onto the next page still belongs to the turn
    # that started on the earlier page
    tanu_turn = turns[2]
    assert tanu_turn["text"] == "Hello Kevin,\nI hope you are doing well."
    assert tanu_turn["page_index"] == 1


def test_parse_turns_with_pages_stops_at_the_footer_across_pages(monkeypatch):
    page_0 = "06:16 AM | Kevin Abraham: hi"
    page_1 = "Exported from JustCall on September 5, 2026 at 03:46 AM"
    monkeypatch.setattr(tpp, "extract_pages_text", lambda pdf_bytes: [page_0, page_1])

    turns = tpp.parse_turns_with_pages(b"irrelevant")
    assert len(turns) == 1
    assert turns[0]["text"] == "hi"


def test_parse_turns_with_pages_matches_parse_turns_on_a_real_single_page_pdf():
    """Sanity check against a real (hand-built) PDF: page-aware parsing
    agrees with the plain string-level parser on everything except the
    added page_index field, which must be 0 for every turn on one page."""
    pdf_bytes = _build_single_page_pdf(SAMPLE_TEXT)
    plain = tpp.parse_turns(tpp.extract_text(pdf_bytes))
    with_pages = tpp.parse_turns_with_pages(pdf_bytes)
    assert len(plain) == len(with_pages)
    for a, b in zip(plain, with_pages):
        assert a["seq"] == b["seq"]
        assert a["speaker"] == b["speaker"]
        assert a["speaker_name"] == b["speaker_name"]
        assert a["text"] == b["text"]
    assert all(t["page_index"] == 0 for t in with_pages)


REAL_SAMPLE_PATH = os.path.expanduser(
    "~/Downloads/justcall_2026_08_19_215475545067923.pdf"
)


@pytest.mark.skipif(
    not os.path.isfile(REAL_SAMPLE_PATH),
    reason="real sample PDF only present on the dev machine that has it",
)
def test_parse_ticket_pdf_against_the_real_sample():
    with open(REAL_SAMPLE_PATH, "rb") as f:
        pdf_bytes = f.read()
    turns = tpp.parse_ticket_pdf(pdf_bytes)
    assert len(turns) > 20
    assert turns[0]["speaker"] == "customer"
    assert turns[0]["speaker_name"] == "Kevin Abraham"
    assert any(t["speaker"] == "bot" and t["speaker_name"] == "Welma Bot" for t in turns)
    assert any(t["speaker"] == "agent" for t in turns)
    seqs = [t["seq"] for t in turns]
    assert seqs == list(range(len(turns)))
    all_text = " ".join(t["text"] for t in turns)
    assert "Exported from JustCall" not in all_text
