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

sent_at (TA-13): each turn's real timestamp, combined from its day
header ("--- August 19, 2026 ---") and its own "HH:MM AM/PM", in the
export's own stated UTC offset ("(GMT+0530)" in the header/footer —
defaults to UTC if that's ever missing). This is the raw data Response
Timeliness (TA-13's new rubric dimension) needs; a datetime.datetime or
None per turn, never a string — callers insert it straight into
ticket_messages.sent_at (TIMESTAMPTZ).
"""

from __future__ import annotations

import io
import re
from datetime import date, datetime, timedelta, timezone

import pdfplumber

_TURN_RE = re.compile(r"^(\d{1,2}:\d{2} (?:AM|PM)) \| (.+?): (.*)$")
_TURN_RE_MULTILINE = re.compile(_TURN_RE.pattern, re.MULTILINE)
_DAY_RE = re.compile(r"^--- (.+) ---$")
_FOOTER_RE = re.compile(r"^Exported from .+ on .+$")
_TZ_OFFSET_RE = re.compile(r"GMT([+-])(\d{2})(\d{2})")
_BOT_NAME = "Welma Bot"
_AGENT_SUFFIX = " from JustCall"
_DAY_DATE_FMT = "%B %d, %Y"
_TURN_TIME_FMT = "%I:%M %p"


def _extract_tz_offset(text: str) -> timezone:
    """The export's own stated UTC offset, e.g. "(GMT+0530)". Defaults to
    UTC if the header/footer doesn't have one — an approximate absolute
    time is still more useful than none at all."""
    m = _TZ_OFFSET_RE.search(text)
    if not m:
        return timezone.utc
    sign, hh, mm = m.groups()
    delta = timedelta(hours=int(hh), minutes=int(mm))
    return timezone(-delta if sign == "-" else delta)


def _parse_day_date(day_text: str) -> date | None:
    try:
        return datetime.strptime(day_text.strip(), _DAY_DATE_FMT).date()
    except ValueError:
        return None


def _parse_turn_datetime(day: date | None, time_text: str, tz: timezone) -> datetime | None:
    if day is None:
        return None
    try:
        parsed_time = datetime.strptime(time_text.strip(), _TURN_TIME_FMT).time()
    except ValueError:
        return None
    return datetime.combine(day, parsed_time, tzinfo=tz)


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
    {seq, speaker, speaker_name, agent_user_id, text, sent_at}.
    speaker is one of 'agent' | 'customer' | 'bot'. sent_at is a
    datetime.datetime (export's own stated UTC offset) or None if the day
    header or time couldn't be parsed.

    Everything before the first turn line (ticket header, Participants,
    Ticket Details) is skipped. A line that isn't a new turn header
    belongs to the previous turn's text — JustCall wraps long messages
    across lines with no other marker. Parsing stops at the export
    footer line so it's never folded into the last turn's text.
    """
    tz = _extract_tz_offset(text)
    turns: list[dict] = []
    current: dict | None = None
    current_date: date | None = None
    seq = 0
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if _FOOTER_RE.match(line):
            break
        if not line.strip():
            continue
        day_match = _DAY_RE.match(line)
        if day_match:
            current_date = _parse_day_date(day_match.group(1)) or current_date
            continue
        m = _TURN_RE.match(line)
        if m:
            if current is not None:
                turns.append(current)
            time_str, raw_name, first_line = m.groups()
            role = _speaker_role(raw_name)
            current = {
                "seq": seq,
                "speaker": role,
                "speaker_name": _speaker_display_name(raw_name, role),
                "agent_user_id": None,
                "text": first_line,
                "sent_at": _parse_turn_datetime(current_date, time_str, tz),
            }
            seq += 1
            continue
        if current is not None:
            current["text"] += "\n" + line
    if current is not None:
        turns.append(current)
    return turns


def extract_pages_text(pdf_bytes: bytes) -> list[str]:
    """Per-page plain text, one entry per page (unlike extract_text(),
    which joins them into one string). Used by parse_turns_with_pages()
    so each turn can be tagged with the page it started on."""
    pages = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")
    return pages


def parse_turns_with_pages(pdf_bytes: bytes) -> list[dict]:
    """Same output as parse_turns() (including sent_at), plus a page_index
    (0-based) on every turn — the page it started on. TA-5 uses this to
    place an extracted embedded image (pypdfium2 reports which page it
    came from) at the right point in the turn sequence, rather than only
    at the very end.
    """
    pages = extract_pages_text(pdf_bytes)
    tz = _extract_tz_offset("\n".join(pages))
    turns: list[dict] = []
    current: dict | None = None
    current_date: date | None = None
    seq = 0
    stopped = False
    for page_index, page_text in enumerate(pages):
        if stopped:
            break
        for raw_line in page_text.split("\n"):
            line = raw_line.rstrip()
            if _FOOTER_RE.match(line):
                stopped = True
                break
            if not line.strip():
                continue
            day_match = _DAY_RE.match(line)
            if day_match:
                current_date = _parse_day_date(day_match.group(1)) or current_date
                continue
            m = _TURN_RE.match(line)
            if m:
                if current is not None:
                    turns.append(current)
                time_str, raw_name, first_line = m.groups()
                role = _speaker_role(raw_name)
                current = {
                    "seq": seq,
                    "speaker": role,
                    "speaker_name": _speaker_display_name(raw_name, role),
                    "agent_user_id": None,
                    "text": first_line,
                    "page_index": page_index,
                    "sent_at": _parse_turn_datetime(current_date, time_str, tz),
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
