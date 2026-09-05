"""Ticket Audit Engine (TA-4): parse a ticket PDF export into structured,
ordered turns ready for ticket_messages (TA-3) and scoring (TA-6).

Per TA-2's finding against a real JustCall export sample: the format is a
consistent, stable, single-platform template (not print-to-PDF / variable),
so this uses the cheap deterministic rule-based parser rather than the
raw-text + LLM-structuring fallback — no Claude call, no per-ticket cost,
and it won't drift since the template is fixed. The LLM-structuring path
for genuinely unknown/variable exports is a different approach for a
different source and is out of scope here.

Confirmed template (verified against a real ticket export):
    Conversation with JustCall
    Ticket ID: #<id>
    Started on <date> at <time> <tz>
    Ticket with JustCall
    Started on ...
    Participants
    <Name> (<email>)
    ...
    Ticket Details
    Ticket ID: #<id>
    Ticket state category: <category>
    Ticket status: <status>
    --- <Month Day, Year> ---
    <HH:MM AM/PM> | <Speaker>: <text, may wrap across lines>
    ...
    --- <Month Day, Year> ---
    ...
    Exported from JustCall on <date> at <time> <tz>

Role classification, from the confirmed sample:
    - agent:    "<Name> from JustCall"
    - bot:      exact name "Welma Bot"
    - customer: any other bare name

No audio, no transcribe.py, no PyAI Hear involved — raw text extraction
only (pdfplumber), an ingestion path independent of the call pipeline.

agent_user_id is always None for now: nothing in this codebase currently
maps a ticket transcript's raw display name (e.g. "Kashif") to a real
user record — org_members has no stored display name to match against,
only a Supabase user_id. The field stays in the output shape so
ticket_messages / TA-6 don't need to change once that resolution exists;
wiring it up is a separate piece of work.
"""

from __future__ import annotations

import io
import re

import pdfplumber

_TURN_RE = re.compile(r"^(\d{1,2}:\d{2} (?:AM|PM)) \| (.+?): (.*)$")
_TURN_RE_MULTILINE = re.compile(_TURN_RE.pattern, re.MULTILINE)
_DAY_RE = re.compile(r"^--- .+ ---$")
_FOOTER_RE = re.compile(r"^Exported from .+ on .+$")
_BOT_NAME = "Welma Bot"
_AGENT_SUFFIX = " from JustCall"


def extract_text(pdf_bytes: bytes) -> str:
    """Full plain text of the PDF, pages joined with a newline."""
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return "\n".join(pages)


def looks_like_justcall_export(text: str) -> bool:
    """Cheap signature check: does this match the known, stable JustCall
    template TA-2 verified, or something else (print-to-PDF, a different
    platform)? Callers should only use parse_turns() when this is True —
    anything else needs the (not yet built) LLM-structuring fallback."""
    return "Ticket Details" in text and bool(_TURN_RE_MULTILINE.search(text))


def _speaker_role(raw_name: str) -> str:
    if raw_name == _BOT_NAME:
        return "bot"
    if raw_name.endswith(_AGENT_SUFFIX):
        return "agent"
    return "customer"


def _speaker_display_name(raw_name: str, role: str) -> str:
    if role == "agent":
        return raw_name[: -len(_AGENT_SUFFIX)]
    return raw_name


def parse_turns(text: str) -> list[dict]:
    """The deterministic parser. Each item:
    {seq, speaker, speaker_name, agent_user_id, text}.
    speaker is one of 'agent' | 'customer' | 'bot'.

    Everything before the first turn line (ticket header, Participants,
    Ticket Details) is skipped. A line that isn't a new turn header
    belongs to the previous turn's text — JustCall wraps long messages
    across lines with no other marker. Parsing stops at the export
    footer line so it's never folded into the last turn's text.
    """
    turns: list[dict] = []
    current: dict | None = None
    seq = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if _FOOTER_RE.match(line):
            break
        if not line.strip() or _DAY_RE.match(line):
            continue
        m = _TURN_RE.match(line)
        if m:
            if current is not None:
                turns.append(current)
            _, raw_name, first_line = m.groups()
            role = _speaker_role(raw_name)
            current = {
                "seq": seq,
                "speaker": role,
                "speaker_name": _speaker_display_name(raw_name, role),
                "agent_user_id": None,
                "text": first_line,
            }
            seq += 1
            continue
        if current is not None:
            current["text"] += "\n" + line
    if current is not None:
        turns.append(current)
    return turns


def parse_ticket_pdf(pdf_bytes: bytes) -> list[dict]:
    """extract_text() + parse_turns(), gated on the known-template check.

    Raises ValueError for a PDF that doesn't match the confirmed JustCall
    template — routing those to a different parsing approach is future
    work (TA-2's second candidate: raw text + one Claude call per ticket),
    not something this deterministic parser should silently guess at.
    """
    text = extract_text(pdf_bytes)
    if not looks_like_justcall_export(text):
        raise ValueError(
            "PDF does not match the known JustCall export template; "
            "this deterministic parser only handles that format."
        )
    return parse_turns(text)
