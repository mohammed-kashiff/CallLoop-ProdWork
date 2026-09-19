# Intercom: how other companies connect to CallLoop

Read this when you recreate the Intercom app, submit it for review, or explain Connect Intercom to someone else.

The CallLoop **code already does OAuth**. You are not rebuilding the integration. You are pointing that code at an Intercom app and, for other companies, getting Intercom to **approve** that app.

Do not put Client IDs, Client Secrets, or access tokens in this file or in git.

---

## What a customer does

1. They have their **own** Intercom (CallLoop signup does not create one).
2. They log into CallLoop → **Integrations** → **Connect Intercom**.
3. Intercom shows a permission screen on **their** workspace. They approve.
4. They land back on Integrations, connected. Closed conversations and tickets can ingest.

That is a button plus Intercom’s yes/no screen. It is not silent.

Command Center can turn this off per org (`enable_intercom_integration`). Disconnect is never blocked by that flag.

---

## What Intercom requires of CallLoop’s app

| Question | Answer |
|---|---|
| Must other companies’ Intercom be reviewed? | **Yes**, if they are not on a workspace your team belongs to. While the app is private, only your team’s workspaces can install it. |
| Must it be in the Intercom App Store? | **No.** After approval it becomes **public unlisted**. Customers install from CallLoop, not from the store. Listing in the store is optional and later. |
| Does CallLoop need a paid Intercom plan so others can Connect? | **No.** They OAuth onto **their** Intercom. CallLoop needs the Developer Hub **app** (Client ID / Secret) to keep existing. |
| Does the customer need Intercom? | **Yes.** If they cancel Intercom, there is nothing to ingest. |

Intercom’s docs: a public app is built from a **development workspace** (free). A paid CallLoop workspace is for your own Messenger / private use, not a requirement for customer Connect.

If the workspace that **hosts** the OAuth app is deleted (unpaid production workspaces can be removed after long inactivity), every customer Connect breaks. Keep that Developer Hub app.

Never collect a customer’s Intercom access token by hand. OAuth only.

---

## Recreate the app (trial closed, company Intercom next)

The trial was only to prove OAuth. A new app in the company Developer Hub is the same product if you point the host at it.

### Change in CallLoop (env)

| Env var | What it is |
|---|---|
| `INTERCOM_CLIENT_ID` | New app’s Client ID |
| `INTERCOM_CLIENT_SECRET` | New app’s Client Secret |

Restart the API after changing them.

Leave these **alone** unless you also move CallLoop’s **own support chat** (Messenger widget), which is a different feature:

- `INTERCOM_MESSENGER_SECRET`
- `VITE_INTERCOM_APP_ID` (frontend)

Optional: `INTERCOM_POLL_SECONDS` (default 300). Not required for a new app.

### Set on the new Intercom app (Developer Hub)

Copy what the trial app had, **except permissions — trim those** (see below). Typical production values (use the real API host):

- OAuth **on**
- Redirect URL: `https://<API_HOST>/api/integrations/intercom/callback`
- Webhook URL: `https://<API_HOST>/api/integrations/intercom/webhook`
- Topics at least: `conversation.admin.closed`, `ticket.resolved`, `ticket.closed`, `app.uninstalled`

#### Trim permissions before submitting ([IN-29](https://calloop.atlassian.net/browse/IN-29))

The trial app has every People / Conversation / Ticket / Workspace-data permission checked — full read+write across all four. The code only ever reads Conversations and Tickets (`backend/intercom_client.py`: `get_conversation`, `search_closed_conversations`, `get_ticket`, `search_closed_tickets`; `backend/intercom_oauth.py`'s `GET /me` for workspace identity on connect). Nothing calls People or Workspace-data, and nothing writes.

On the new app, under Authentication → Permissions:

- Keep: Conversations (read), Tickets (read)
- Uncheck: write access on both, plus the full People and Workspace-data categories

This is a dashboard-only change — `build_authorize_url()` doesn't send a `scope` param, so Intercom grants exactly whatever's checked here. Over-broad scope requests are a common reason review gets bounced, so do this before submitting, not after. Confirm no org besides CallLoop's own is connected before flipping it, so no live connection breaks.

No new CallLoop tables. No Connect-button rewrite. Still **one** CallLoop app, many customer workspaces.

### After you switch

Anyone who connected the **old/trial** app must click **Connect Intercom** again. Old stored tokens belong to the dead Client ID.

A new app is private until **that** app is reviewed. Putting it on the company paid Intercom does not let random customers install.

If the company Intercom is **EU or AU**, Google login on OAuth may need that region’s authorize host. CallLoop currently sends people to `https://app.intercom.com/oauth` (US). Email/password can still pick region; Google on the wrong host fails. That is the one likely **code** change, and only if you are not US.

---

## After Intercom approves

Customers use the same Integrations button. You do not join their workspace. You do not need the App Store.

Approval is for the app you submitted. Recreating on another Intercom account later is a **new** app and needs its **own** review.

For review, Intercom will want: a short description, a **test CallLoop login**, and a video of Connect → use → disconnect. Show the `client_id` and `state` on the authorize URL. Trim permissions first ([IN-29](https://calloop.atlassian.net/browse/IN-29) — see above).

Keep a separate `[Dev]` / `[Staging]` Intercom app for experiments so production stays frozen. Scope or OAuth changes on the live app need re-approval.

---

## Related

- Pilot ingest checklist: [in15-pilot-runbook.md](in15-pilot-runbook.md)
- Code: `backend/intercom_oauth.py`, `backend/api.py` (`/api/integrations/intercom/*`), `frontend/src/pages/Integrations.tsx`
- Messenger (not this flow): `backend/intercom_widget.py`
