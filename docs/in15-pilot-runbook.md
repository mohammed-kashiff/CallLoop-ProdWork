# IN-15 pilot verification runbook

One real Intercom workspace, one real closed conversation, run end-to-end and checked by hand against all 8 acceptance criteria on the epic (Jira [IN-15](https://calloop.atlassian.net/browse/IN-15)). This is a manual pilot, not a test suite — use this doc as the checklist while running it.

**No manual poll trigger exists for Intercom** (unlike JustCall's `POST /api/integrations/justcall/sync`). To force ingestion instead of waiting for `INTERCOM_POLL_SECONDS` (default 300s), either wait for a poll cycle, or send the real webhook the conversation triggers on close (`conversation.admin.closed`) to `POST /api/integrations/intercom/webhook` — Intercom sends this itself once the conversation is actually closed in their UI, so normally no manual send is needed.

Pick or arrange **two** conversations in the connected workspace before starting:
- **Conversation A**: closed, no linked ticket, no notes, no attachment, single agent. (Covers #1, #2.)
- **Conversation B**: closed, linked to an Intercom ticket that has other `linked_objects`, at least one internal note, at least one screenshot attachment, and ideally more than one agent touching it (one whose email you've already aliased in `ticket_agent_identity_aliases`, one you haven't). (Covers #3–#7.)

For each item below: query the DB directly with `psql "$DATABASE_URL"` or hit the API with `curl` + your session cookie/JWT — don't rely on the manager UI alone, since two of the eight criteria aren't rendered anywhere in the UI (noted inline).

## 1 — OAuth connect, token retrievable on next ingestion

- Connect: Settings → Integrations → Intercom → Connect, complete OAuth.
- `GET /api/integrations/intercom` → check `"configured": true`.
- `SELECT provider, external_account_id, key_suffix FROM org_credentials WHERE org_id = '<org>' AND provider = 'intercom';` → one row.
- After the next poll cycle or webhook delivery, grep logs for `event=intercom_callback accepted=True org_id=...` (confirms the vault round-trip worked); an `accepted=False reason=...` line means the token didn't load.

## 2 — Standalone closed conversation, ingested + scored within one cycle

- Close Conversation A in Intercom, wait one poll cycle (≤ `INTERCOM_POLL_SECONDS`).
- `SELECT id, status, source, created_at FROM tickets WHERE org_id = '<org>' AND source = 'intercom_api' ORDER BY created_at DESC LIMIT 5;` → new row, `status = 'ready'`.
- `SELECT seq, speaker, text FROM ticket_messages WHERE ticket_id = '<id>' ORDER BY seq;` → turns present.
- Log line: `event=intercom_ingest result=ok kind=conversation ... group_size=1`.
- `POST /api/tickets/<id>/score` → 200 with a non-empty `findings` array.

## 3 — Linked-ticket conversation merges into one case

- Close Conversation B (with its linked ticket already set up in Intercom).
- `SELECT id, external_id FROM tickets WHERE org_id = '<org>' AND source = 'intercom_api' ORDER BY created_at DESC LIMIT 5;` — the merged case is **one row** with `external_id` like `group:<lowest-numeric-id>`, not two separate `conversation:<id>` / `ticket:<id>` rows.
- `SELECT speaker, text, sent_at FROM ticket_messages WHERE ticket_id = '<id>' ORDER BY seq;` — turns from both the conversation and every linked object are interleaved by `sent_at`, not two disjoint blocks.
- Log line: `event=intercom_ingest kind=group group_size=<N>` where N ≥ 2.

## 4 — Internal notes tagged and excluded from customer-facing scoring

- `SELECT seq, speaker, is_internal, text FROM ticket_messages WHERE ticket_id = '<id>' ORDER BY seq;` — the note turn has `is_internal = true`.
- **Not visible in the manager UI** — `TicketAudit.tsx` doesn't render an internal badge, so this is a DB/API-only check: `GET /api/tickets/<id>` also returns `is_internal` per message in its `messages` array.
- Confirm the customer-facing-only rubric dimensions in the score response don't cite that turn's `seq` as evidence.

## 5 — Every agent mapped or flagged unmapped

- `SELECT identifier, user_id FROM ticket_agent_identity_aliases WHERE org_id = '<org>' AND provider = 'intercom';` — the agent you pre-aliased shows up with a `user_id`.
- `SELECT seq, speaker, agent_user_id FROM ticket_messages WHERE ticket_id = '<id>' AND speaker = 'agent' ORDER BY seq;` — the aliased agent's turns have `agent_user_id` set; the un-aliased agent's turns have `agent_user_id IS NULL` (that's the "flagged as unmapped" signal — there's no separate UI badge for it, this is the check).

## 6 — Screenshot appears, described, in correct position

- `GET /api/tickets/<id>` → in `messages`, the turn right after the one that carried the attachment has `has_image: true` and `text` holding Claude's description.
- Open the ticket in the manager UI (`/ticket-audit/<id>`) — `TicketAudit.tsx` renders the image inline via `GET /api/tickets/<id>/assets/<seq>`; confirm it displays where the conversation actually attached it, not at the end.

## 7 — audit_summary reflects CallLoop's own scoring, not Intercom's recap

- **Not rendered anywhere in the manager UI** — `top_strength`/`top_gap`/`audit_summary` are computed fresh on every `POST /api/tickets/<id>/score` call and returned in that JSON response, but never persisted to `ticket_audits` and never read by the frontend. Verify via the raw response: open browser devtools → Network tab while loading the ticket (the page itself calls this endpoint), or `curl -X POST .../api/tickets/<id>/score` directly, and read the `audit_summary` field by eye — it should read as an evaluative sentence about strengths/gaps, not a paraphrase of the conversation itself.

## 8 — Manager view indistinguishable from a PDF-sourced audit

- Open Conversation B's audit and any existing PDF-sourced audit in the manager UI side by side (or one after another) — both render through the same `TicketAudit.tsx` component and the same `GET /api/tickets/<id>` + `POST /api/tickets/<id>/score` calls regardless of `tickets.source`. Confirm layout, evidence-quote rendering, and per-agent breakdown look identical; the only place `source` is ever labeled at all is the list view (`Audits.tsx`), not the scoring page itself.

## After the pilot

Record the outcome per criterion (pass/fail + note) as a comment on IN-15, then transition it to Done only if all 8 hold up. If anything fails, file it as a Bug in the `IN` project the same way [IN-19](https://calloop.atlassian.net/browse/IN-19) was — don't fold a real regression into IN-15's own closing comment.
