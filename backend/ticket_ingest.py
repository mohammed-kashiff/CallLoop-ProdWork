"""Ticket Audit Engine write path (TA-4 text turns, TA-5 embedded images):
persist a parsed ticket PDF into tickets / ticket_messages (TA-3,
alembic/versions/0022_tickets.py) and ticket_message_assets (TA-5,
0023_ticket_image_assets.py).

Independent of the call-scoring engine and of transcribe.py — no audio,
no PyAI Hear. Writes go through org_scope()/db.connection(), the same
RLS-safe pattern as every other tenant write in this codebase — the
session role is never granted a way around row-level security.

ticket_messages.agent_user_id is nullable and only ever set when the
caller already has a resolved org_members.user_id for that turn's
speaker — ticket_pdf_parser never returns one today (see that module's
docstring for why), so every write here is NULL until an
agent-identity-resolution step exists upstream.

Note: the parser also returns speaker_name (the raw display name off the
PDF, e.g. "Kashif") for turns where agent_user_id can't be resolved.
ticket_messages (TA-3) has no column to hold that, so it is intentionally
dropped here rather than persisted — flagged for whoever picks up TA-8
(multi-agent attribution), not something to fix by altering TA-3's schema
unilaterally.

TA-5 design (PRD §8.1): an embedded screenshot is extracted at ingest
time, described with one Claude vision call, and injected into the same
turn sequence as a normal turn — scoring (TA-6) needs zero changes, since
it already just reads a sequenced text stream. interleave_images() places
each image right after the last text turn on the same or an earlier page
(ticket_pdf_parser.parse_turns_with_pages() tags each turn with the page
it started on; pypdfium2 reports which page an image came from) and
inherits that turn's speaker — the closest available signal for "whose
message this screenshot was attached to" without exact on-page
coordinates. The original bytes are kept as a viewable asset (Supabase
Storage, ticket_image_store.py) so evidence verification can point a
reviewer at the real picture instead of a re-typed quote.
"""

from __future__ import annotations

from . import db
from . import ticket_image_extraction
from . import ticket_image_store
from . import ticket_pdf_parser
from .org_ids import org_scope


def create_ticket(org_id: str, *, source: str = "pdf_upload") -> str:
    """One row in `tickets`. status defaults to 'uploaded'. Returns the new id."""
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO tickets (org_id, source)
                VALUES (%s, %s)
                RETURNING id
                """,
                (org_id, source),
            ).fetchone()
    return str(row["id"])


def set_ticket_status(ticket_id: str, org_id: str, status: str) -> None:
    with org_scope(org_id):
        with db.connection() as conn:
            conn.execute(
                "UPDATE tickets SET status = %s WHERE id = %s AND org_id = %s",
                (status, ticket_id, org_id),
            )


def insert_ticket_messages(ticket_id: str, org_id: str, turns: list[dict]) -> None:
    """Bulk-insert parsed turns (ticket_pdf_parser's output shape) into
    ticket_messages, in seq order. No-op for an empty list."""
    if not turns:
        return
    with org_scope(org_id):
        with db.connection() as conn:
            for t in turns:
                conn.execute(
                    """
                    INSERT INTO ticket_messages
                        (ticket_id, org_id, seq, agent_user_id, speaker, text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        ticket_id, org_id, t["seq"], t["agent_user_id"],
                        t["speaker"], t["text"],
                    ),
                )


def insert_ticket_message_assets(ticket_id: str, org_id: str, assets: list[dict]) -> None:
    """Bulk-insert asset metadata rows (ticket_message_assets, TA-5) — one
    per stored screenshot, pointing at its Storage object. Each item:
    {seq, width, height, storage_key}. No-op for an empty list."""
    if not assets:
        return
    with org_scope(org_id):
        with db.connection() as conn:
            for a in assets:
                conn.execute(
                    """
                    INSERT INTO ticket_message_assets
                        (ticket_id, org_id, seq, width, height, storage_key)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (ticket_id, org_id, a["seq"], a["width"], a["height"], a["storage_key"]),
                )


def interleave_images(turns: list[dict], images: list[dict], descriptions: list[str]) -> list[dict]:
    """Merge extracted embedded images into a page-tagged turn sequence
    (ticket_pdf_parser.parse_turns_with_pages() output) into one combined,
    sequentially-renumbered turn list (TA-5).

    Each image is placed right after the last turn on the same or an
    earlier page and inherits that turn's speaker — screenshots belong to
    whichever message attached them, and "the turn right before it on the
    same page" is the closest signal available without exact on-page
    coordinates. Images before any turn on their page (or when there are
    no turns at all) fall back to the first turn's speaker, or 'customer'
    if there are no turns.

    Every image turn carries is_image=True plus width/height/png_bytes so
    callers can store it as a viewable asset without re-deriving it.
    """
    def _image_turn(image: dict, description: str, speaker: str,
                     speaker_name: str, agent_user_id) -> dict:
        return {
            "speaker": speaker,
            "speaker_name": speaker_name,
            "agent_user_id": agent_user_id,
            "text": description,
            "is_image": True,
            "png_bytes": image["png_bytes"],
            "width": image["width"],
            "height": image["height"],
        }

    result: list[dict] = []
    img_idx = 0
    n_images = len(images)

    if not turns:
        for image, desc in zip(images, descriptions):
            result.append(_image_turn(image, desc, "customer", "customer", None))
        for i, t in enumerate(result):
            t["seq"] = i
        return result

    last_speaker = turns[0]["speaker"]
    last_speaker_name = turns[0]["speaker_name"]
    last_agent_user_id = turns[0]["agent_user_id"]
    n_turns = len(turns)

    for i, turn in enumerate(turns):
        result.append({**turn, "is_image": False})
        last_speaker = turn["speaker"]
        last_speaker_name = turn["speaker_name"]
        last_agent_user_id = turn["agent_user_id"]
        # Flush pending images only once we've seen every turn on this
        # page — not after the first turn that merely reaches it — so an
        # image shares a page with several turns lands after the last one.
        is_last_turn_on_its_page = (
            i + 1 == n_turns or turns[i + 1]["page_index"] > turn["page_index"]
        )
        if not is_last_turn_on_its_page:
            continue
        while img_idx < n_images and images[img_idx]["page_index"] <= turn["page_index"]:
            result.append(_image_turn(
                images[img_idx], descriptions[img_idx],
                last_speaker, last_speaker_name, last_agent_user_id,
            ))
            img_idx += 1

    while img_idx < n_images:
        result.append(_image_turn(
            images[img_idx], descriptions[img_idx],
            last_speaker, last_speaker_name, last_agent_user_id,
        ))
        img_idx += 1

    for i, t in enumerate(result):
        t["seq"] = i
    return result


def ingest_ticket_pdf(org_id: str, pdf_bytes: bytes, *, source: str = "pdf_upload") -> str:
    """Full write path: create the ticket row, parse the PDF against the
    confirmed JustCall template (TA-2) into page-tagged text turns,
    extract and describe any embedded images (TA-5), interleave everything
    into one ordered turn sequence, write it to ticket_messages, store
    each image as a viewable asset, and move the ticket to 'ready' for
    scoring (TA-6). Any failure — unrecognized PDF format, a vision-call
    error, a storage error — marks the ticket 'failed' and re-raises;
    nothing partial is left looking like a successful ingest.
    """
    ticket_id = create_ticket(org_id, source=source)
    set_ticket_status(ticket_id, org_id, "processing")
    try:
        text = ticket_pdf_parser.extract_text(pdf_bytes)
        if not ticket_pdf_parser.looks_like_justcall_export(text):
            raise ValueError(
                "PDF does not match the known JustCall export template; "
                "this deterministic parser only handles that format."
            )
        turns = ticket_pdf_parser.parse_turns_with_pages(pdf_bytes)

        images = ticket_image_extraction.extract_images(pdf_bytes)
        descriptions = [ticket_image_extraction.describe_image(img["png_bytes"]) for img in images]
        merged = interleave_images(turns, images, descriptions)

        insert_ticket_messages(ticket_id, org_id, merged)

        assets = []
        for turn in merged:
            if not turn.get("is_image"):
                continue
            key = ticket_image_store.put_bytes(org_id, ticket_id, turn["seq"], turn["png_bytes"])
            assets.append({
                "seq": turn["seq"], "width": turn["width"], "height": turn["height"],
                "storage_key": key,
            })
        insert_ticket_message_assets(ticket_id, org_id, assets)
    except Exception:
        set_ticket_status(ticket_id, org_id, "failed")
        raise

    set_ticket_status(ticket_id, org_id, "ready")
    return ticket_id


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def list_tickets(org_id: str) -> list[dict]:
    """Org-scoped ticket library rows. No message bodies."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT t.id, t.source, t.status, t.created_at,
                       (SELECT COUNT(*) FROM ticket_messages m
                          WHERE m.ticket_id = t.id AND m.org_id = t.org_id)
                         AS message_count,
                       (SELECT ta.score FROM ticket_audits ta
                          WHERE ta.ticket_id = t.id AND ta.org_id = t.org_id)
                         AS audit_score
                FROM tickets t
                WHERE t.org_id = %s
                ORDER BY t.created_at DESC
                """,
                (org_id,),
            ).fetchall()
    return [
        {
            "id": str(r["id"]),
            "source": r["source"],
            "status": r["status"],
            "created_at": _iso(r["created_at"]),
            "message_count": int(r["message_count"] or 0),
            "has_audit": r["audit_score"] is not None,
            "score": r["audit_score"],
        }
        for r in rows
    ]


def get_ticket(ticket_id: str, org_id: str) -> dict | None:
    """One ticket plus its turns and screenshot-asset metadata.

    storage_key stays server-side — callers that need the picture go
    through the signed-URL route, not this payload.
    """
    with org_scope(org_id):
        with db.connection() as conn:
            ticket = conn.execute(
                """
                SELECT id, source, status, created_at
                FROM tickets
                WHERE id = %s AND org_id = %s
                """,
                (ticket_id, org_id),
            ).fetchone()
            if not ticket:
                return None
            messages = conn.execute(
                """
                SELECT seq, speaker, text, agent_user_id, sent_at
                FROM ticket_messages
                WHERE ticket_id = %s AND org_id = %s
                ORDER BY seq
                """,
                (ticket_id, org_id),
            ).fetchall()
            assets = conn.execute(
                """
                SELECT seq, width, height, content_type
                FROM ticket_message_assets
                WHERE ticket_id = %s AND org_id = %s
                ORDER BY seq
                """,
                (ticket_id, org_id),
            ).fetchall()
            audit_row = conn.execute(
                """
                SELECT score, findings, created_at
                FROM ticket_audits
                WHERE ticket_id = %s AND org_id = %s
                """,
                (ticket_id, org_id),
            ).fetchone()
    asset_seqs = {int(a["seq"]) for a in assets}
    return {
        "id": str(ticket["id"]),
        "source": ticket["source"],
        "status": ticket["status"],
        "created_at": _iso(ticket["created_at"]),
        "messages": [
            {
                "seq": int(m["seq"]),
                "speaker": m["speaker"],
                "text": m["text"],
                "agent_user_id": str(m["agent_user_id"]) if m["agent_user_id"] else None,
                "sent_at": _iso(m["sent_at"]),
                "has_image": int(m["seq"]) in asset_seqs,
            }
            for m in messages
        ],
        "assets": [
            {
                "seq": int(a["seq"]),
                "width": int(a["width"]),
                "height": int(a["height"]),
                "content_type": a["content_type"],
            }
            for a in assets
        ],
        "audit": None if not audit_row else {
            "score": audit_row["score"],
            "created_at": _iso(audit_row["created_at"]),
            **(audit_row["findings"] if isinstance(audit_row["findings"], dict)
               else {}),
        },
    }


def ticket_asset_meta(ticket_id: str, org_id: str, seq: int) -> dict | None:
    """RLS-scoped existence check for one screenshot row. No storage_key."""
    with org_scope(org_id):
        with db.connection() as conn:
            row = conn.execute(
                """
                SELECT seq, width, height, content_type
                FROM ticket_message_assets
                WHERE ticket_id = %s AND org_id = %s AND seq = %s
                """,
                (ticket_id, org_id, seq),
            ).fetchone()
    if not row:
        return None
    return {
        "seq": int(row["seq"]),
        "width": int(row["width"]),
        "height": int(row["height"]),
        "content_type": row["content_type"],
    }


def list_ticket_ids_for_agent(org_id: str, agent_user_id: str) -> list[str]:
    """TA-12: every ticket this agent has at least one turn on, most
    recent first — the input to their own cross-ticket rollup view."""
    with org_scope(org_id):
        with db.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT t.id, t.created_at
                FROM tickets t
                JOIN ticket_messages m
                  ON m.ticket_id = t.id AND m.org_id = t.org_id
                WHERE t.org_id = %s AND m.agent_user_id = %s
                ORDER BY t.created_at DESC
                """,
                (org_id, agent_user_id),
            ).fetchall()
    return [str(r["id"]) for r in rows]
