
CallProof scores support calls against a soft-skills rubric. Agents (or managers) upload recordings; the stack transcribes them with **PyAI Hear**, evaluates them with **Claude** (and hybrid rules), then shows a scorecard, churn signals, coaching-style feedback, and a review queue in the **Call Loop** UI.

Public repo: **[CallLoop](https://github.com/mohammed-kashiff/CallLoop)** · branch **`main`**.  
This working tree is **callproof `v2testing-ui-final`** (same codebase when those branches are synced).

<p align="center">
  <img src="docs/call-loop-hackathon.png" alt="Call Loop — PyAI Hackathon · Team Foursight" width="720" />
</p>

---

## What the product does

- **Ingest** call audio (single file or bulk zip, up to 100 files)
- **Transcribe** with PyAI Hear (speaker-labelled async jobs on a live key)
- **Score** against rubric v8 (Resolution, Ownership, Tone/Empathy/Professionalism, etc.)
- **Surface** scorecards, churn risk, areas of improvement, stakeholder email drafts
- **Review** flagged calls (pending / solved)
- **JustCall** — completed calls are pulled, transcribed, and scored automatically (Integrations tab)
- **Estimate** approximate PyAI + Claude spend (tunable rates; not an invoice)

UI brand in this branch: **Call Loop v3** (React + TypeScript + Vite).  
**Training** in the sidebar is a placeholder (“Coming soon”) — not wired yet.

---

## How it functions

```text
┌─────────────┐     ┌──────────────────┐     ┌─────────────────┐
│  Call Loop  │────▶│  CallProof API   │────▶│ Postgres + audio│
│  UI :5173   │◀────│  FastAPI :8000   │◀────│  (DATABASE_URL) │
└─────────────┘     └────────┬─────────┘     └─────────────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
        ┌──────────┐  ┌───────────┐  ┌────────────┐
        │ PyAI Hear│  │PyAI Recap │  │  Claude    │
        │transcribe│  │ (optional)│  │  (Anthropic)│
        └──────────┘  └───────────┘  └────────────┘
```

### Pipeline (one call)

1. **Upload** — UI sends audio to `POST /api/upload` (or batch zip).
2. **Hear copy** — browser/server prepare telephony-friendly audio when needed.
3. **Transcribe** — PyAI async job → speaker segments stored in Postgres.
4. **Audit** — `GET /api/calls/{id}/audit` runs hybrid/full QA (rules + Claude).
5. **Report** — score, grade, findings, churn; on-demand feedback / email / flag for review.

<img width="402" height="737" alt="image" src="https://github.com/user-attachments/assets/c1678a7e-f90a-4971-beb9-339aa0a1133a" />

### Main pieces

| Layer | Role |
|-------|------|
| `frontend/` | Call Loop UI (pages, sidebar, upload queue, review) |
| `backend/api.py` | FastAPI routes (upload, audit, flag/solve, export, status) |
| `backend/transcribe.py` | PyAI Hear jobs |
| `backend/qa_engine.py` + `backend/rules_v8.py` + `rubric.json` | Scoring (runtime rubric is `rubric.json`) |
| `backend/pyai_usage.py` + `backend/cost_estimate.py` | Local usage + $ estimates |
| `callproof.db` / `audio/` / `logs/` | Data, playback, app logs |

---

## Sandbox vs live PyAI key

| | Sandbox (`pyai_test_…`) | Live (`pyai_live_…`) |
|--|-------------------------|----------------------|
| How you get it | Auto-minted on first API start if `.env` has no key, **or** set manually | [console.pyai.com](https://console.pyai.com) |
| Typical scopes | `hear:transcribe` (sync text) | `transcribe:jobs` (+ Recap when enabled) |
| CallProof full QA | **Limited** — diarized async jobs / Recap often fail | **Required** for production-like scoring |

Sandbox is fine to **boot the stack and explore the UI**. For real transcription + scorecards, put a **live** `PYAI_API_KEY` in `.env`.

You always need an **`ANTHROPIC_API_KEY`** for Claude scoring — paste your real key from the Anthropic console (do not commit it).

> **Sandbox key minting returns 429?**  
> Your network has hit PyAI's sandbox key limit (rate limited per IP/network).  
> Switch to a different internet connection (e.g. phone hotspot) and restart,  
> or add a live `PYAI_API_KEY` from [console.pyai.com](https://console.pyai.com) to `.env` manually.

---

## Install & run (every terminal command)

Use **two terminal tabs**. Run commands from the **repository root** (this folder).

### Prerequisites

- Git  
- Python **3.11+** (3.12 fine)  
- Node.js **20+** and npm  
- Internet (PyAI + Anthropic)  
- Optional: system **ffmpeg** if browser/server Hear transcodes fail on your machine (`imageio-ffmpeg` is already a Python dependency for many paths)

Check:

```bash
git --version
python3 --version
node --version
npm --version
```

---

### A. Clone and enter the repo

```bash
git clone https://github.com/mohammed-kashiff/CallLoop
cd CallLoop
git checkout main
git pull origin main
```

If you already have the repo:

```bash
cd ~/CallLoop
git checkout main
git pull origin main
```

---

### B. Python backend (sandbox-friendly)

```bash
cd ~/CallLoop
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Create env file:

```bash
cp .env.example .env
```

Edit `.env` (any editor — on macOS you can use `open -e .env`). Minimum for Claude scoring:

```bash
# Required for QA — paste your real Anthropic key (never commit .env)
ANTHROPIC_API_KEY=

# Leave blank to auto-mint a PyAI sandbox key on first API start,
# OR paste a sandbox/live key yourself:
PYAI_API_KEY=

AUDIT_MODE=hybrid

# Supabase Auth (required for login — never commit real values)
SUPABASE_URL=
SUPABASE_JWT_SECRET=
```

Also copy `frontend/.env.example` to `frontend/.env` and set `VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`.

Optional spend-estimate knobs (already in `.env.example`):

```bash
COST_PYAI_USD_PER_MINUTE=0.01
COST_PYAI_USD_PER_UNIT=0.01
COST_CLAUDE_USD_PER_AUDIT=0.06
COST_CLAUDE_USD_PER_HIT=0.02
```

**Never commit `.env`.**

Start the API (**Terminal 1** — leave it running):

```bash
cd ~/CallLoop
source .venv/bin/activate
uvicorn backend.api:app --reload --port 8000
```

`uvicorn api:app --reload --port 8000` still works (root shim). Prefer `backend.api:app` so `--reload` watches the package.

On first start with an empty `PYAI_API_KEY`, the API tries to mint a free sandbox key and write it into `.env`. Watch the terminal for:

- `No PYAI_API_KEY found — minting a free sandbox key...`
- or `PYAI_API_KEY present (sandbox key)` / `(configured key)`

> **If you see `sandbox key minting failed (HTTP 429)`:**  
> Your network has hit PyAI's sandbox key limit. Switch to a phone hotspot  
> and restart uvicorn, or add a live key to `.env` manually (see Section G).

API base: **http://127.0.0.1:8000**

Quick check (optional, new tab):

```bash
curl -s http://127.0.0.1:8000/api/calls | head
curl -s http://127.0.0.1:8000/api/pyai/status | head
```

---

### C. Frontend — Call Loop UI

**Terminal 2:**

```bash
cd ~/CallLoop/frontend
npm install
npm run dev
```

UI: **http://127.0.0.1:5173**

Open that URL in your browser.

Click the **Sandbox / Live** chip in the sidebar (or the same label in the top bar) to paste a live PyAI key and/or a Claude key. They are saved to `.env` and take effect immediately — the chip switches to **Live** when the PyAI key starts with `pyai_live_`.

---

### D. Day-to-day restart (after install)

**Terminal 1 — API**

```bash
cd ~/CallLoop
source .venv/bin/activate
uvicorn backend.api:app --reload --port 8000
```

`uvicorn api:app --reload --port 8000` still works (root shim). Prefer `backend.api:app` so `--reload` watches the package.

**Terminal 2 — UI**

```bash
cd ~/CallLoop/frontend
npm run dev
```

---

### E. Update to latest `main`

```bash
cd ~/CallLoop
git checkout main
git pull origin main
source .venv/bin/activate
pip install -r requirements.txt
cd frontend
npm install
```

Then restart API + UI as in section D.

---

### F. Free ports if something is already bound

```bash
lsof -tiTCP:8000 -sTCP:LISTEN | xargs kill
lsof -tiTCP:5173 -sTCP:LISTEN | xargs kill
```

Then start API and UI again.

---

### G. Optional: use a live PyAI key (full transcription)

1. Create a live key at [console.pyai.com](https://console.pyai.com) with `transcribe:jobs`.
2. Put it in `.env`:

```bash
PYAI_API_KEY=pyai_live_your_key_here
```

3. Restart uvicorn (Ctrl+C in Terminal 1, then start again).

---

### H. Optional: JustCall auto-ingest

Completed JustCall calls are downloaded, transcribed with Hear, scored with Claude, and listed under **Integrations**.

1. In JustCall → **Settings → APIs and Webhooks**, copy the API key and secret.
2. On **Integrations**, paste both and click **Save and connect** (also available in the API keys panel).
3. Click **Sync now**. Finished calls appear on that page. New ones are pulled automatically after that.

You do not need a webhook or ngrok on this laptop.

---

## Deploy the API (Render)

Public HTTPS API (this workspace): **https://callloop-prodwork.onrender.com**

Health (must return HTTP 200 and `{"ok":true}`):

```bash
curl -sf https://callloop-prodwork.onrender.com/health
```

The live Web Service is named **callloop-prodwork**. It builds from **`CallLoop-ProdWork`** on branch **`CallLoop-build`**. Render auto-deploys that branch. Service settings (start command, health path, env var *names*) also live in [`render.yaml`](render.yaml).

### Repeatable deploy (CI trigger)

```bash
git checkout CallLoop-build
git push callloop-prodwork CallLoop-build
```

Watch **callloop-prodwork → Events** until the deploy is Live, then run the `curl` above.

Dashboard fallback: open the service → **Manual Deploy → Deploy latest commit**.

### Host settings (already applied on the service)

| Field | Value |
|-------|--------|
| Runtime | Python |
| Build | `pip install -r requirements.txt` |
| Start | `uvicorn backend.api:app --host 0.0.0.0 --port $PORT` |
| Health check | `/health` |

Set these as **Environment** variables on the Web Service (not Secret Files, not on the Static Site):

| Name | Notes |
|------|--------|
| `PYAI_API_KEY` | Live key `pyai_live_…` |
| `ANTHROPIC_API_KEY` | Claude scoring |
| `CORS_ORIGINS` | `https://callloop-web.onrender.com` (comma-separate extra origins if needed) |
| `DATABASE_URL` | Supabase Postgres URI (password stays in the dashboard). Alias: `SUPABASE_DB_URL`. |
| `SUPABASE_URL` | Project URL (`https://<ref>.supabase.co`). JWT issuer is `{URL}/auth/v1`. |
| `SUPABASE_JWT_SECRET` | Project Settings → API JWT secret (HS256). Dashboard-only (`sync: false`). |

On the **Static Site** (`callloop-web`), set **build-time** env: `VITE_SUPABASE_URL`, `VITE_SUPABASE_ANON_KEY`, and `VITE_API_URL` (the API origin). Vite bakes these in at build; changing them later needs a rebuild.

### Auth (CL-8)

The UI signs up / logs in with **Supabase Auth**. The session is stored in the browser and refreshed automatically. Every data request sends `Authorization: Bearer <access_token>` (not cookies — no CSRF). The API verifies the JWT (HS256 via `SUPABASE_JWT_SECRET`, or ES256/RS256 via JWKS at `{SUPABASE_URL}/auth/v1/.well-known/jwks.json`), then upserts **org membership**.

- **First authenticated request** (empty `org_members`): owner of the placeholder org `00000000-0000-4000-8000-000000000001` (keeps existing Table Editor rows).
- **Later signups**: a new org, that user as owner, plus a seeded legacy v8 rubric.
- Unauthenticated calls to data routes return **401**. `/health` and the JustCall webhook stay public.
- **Isolation (CL-9):** every read and write uses `org_id` from the verified JWT (`request.state.org_id`). Query params, path, and JSON bodies cannot set it. JustCall webhook/poller use `JUSTCALL_ORG_ID` (or the placeholder org), never a payload field. Code review: `.cursor/rules/org-isolation.mdc`.
- **RLS (CL-10):** second layer. Alembic `0005_rls` enables RLS on `orgs`, `calls`, `segments`, `audits`, `rubrics`, `api_usage`. The API does `SET LOCAL ROLE callproof_app` (`NOBYPASSRLS`) then `SET LOCAL app.current_org_id` from the JWT org. `DATABASE_URL` may stay the postgres URI (Alembic needs bypass). Do not apply policies by hand in the dashboard. `org_members` is not RLS’d (signup bootstrap).

Local: copy `SUPABASE_URL` / `SUPABASE_JWT_SECRET` into `.env`, and `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` into `frontend/.env`. Enable email auth in the Supabase dashboard.

Optional: `JUSTCALL_API_KEY`, `JUSTCALL_API_SECRET`, `JUSTCALL_WEBHOOK_SECRET`. Never commit values.

### Postgres schema and Alembic (CL-4 / CL-5)

The API uses **Postgres** at runtime (`DATABASE_URL` / `SUPABASE_DB_URL`). **Do not** add columns in Python at boot. Schema is versioned in `alembic/versions/`.

`0001_orgs_calls` — `orgs`, `calls`, `segments`, `audits` (`org_id NOT NULL`).  
`0002_api_usage` — `api_usage` with `org_id`.  
`0003_rubrics_audits` — `rubrics`; recreates `audits` with surrogate `id` and `UNIQUE (call_id, rubric_id, rubric_version)`. Seeds one **"Default (legacy v8)"** rubric per org from `rubric.json`. **Does not backfill** old scorecards.  
`0004_org_members` — `org_members` (`user_id` = Supabase JWT `sub`, `UNIQUE (user_id)` so one org per user at launch).  
`0005_rls` — **ENABLE ROW LEVEL SECURITY** on `orgs`, `calls`, `segments`, `audits`, `rubrics`, `api_usage`, with SELECT/INSERT/UPDATE/DELETE policies keyed on `app.current_org_id`. Creates `callproof_app` (`NOLOGIN`, `NOBYPASSRLS`). The API `SET LOCAL ROLE`s to it so RLS actually applies even when `DATABASE_URL` is postgres. Policies live in this revision — do not toggle them in the Table Editor.

**Service-role bypass (limited, and verified):** `postgres` / `service_role` skip RLS. Use those connections only for `alembic upgrade` and one-off backfill (`db.connection(bypass_rls=True)`). CL-10 AC: an API `db.connection()` session has `current_user = callproof_app` and `rolbypassrls = false`. Raw `psycopg.connect` as postgres does **not** count. `org_members` is not RLS’d (first-user claim reads it before the org GUC is set).

`0006_rls_role_grant` — `GRANT callproof_app TO CURRENT_USER` so pooler postgres (not superuser) can `SET ROLE`. Idempotent if 0005 already granted it.

Placeholder org: `00000000-0000-4000-8000-000000000001`. Legacy rubric id: `00000000-0000-4000-8000-000000000011`.

New calls and audits write to Postgres (that org). Local `callproof.db` is unused. Do not mutate a seeded rubric in place; bump `version` and insert a new row.

**Apply** (explicit step, not on API import). Prefer session pooler `aws-0-us-west-2.pooler.supabase.com:5432` if `db.<ref>.supabase.co` does not resolve:

```bash
# DATABASE_URL in .env — do not paste the URI into chat or git
unset DATABASE_URL
source .venv/bin/activate
pip install -r requirements.txt
alembic upgrade head
```

**Create a new migration** after you change the model:

```bash
alembic revision -m "short_description"
# edit the new file under alembic/versions/
alembic upgrade head
```

`render.yaml` sets **Pre-Deploy Command** `alembic upgrade head` so Render applies migrations on deploy, not when uvicorn loads.

Confirm in **Table Editor**: `orgs`, `calls`, `segments`, `audits`, `api_usage`, `rubrics`, `org_members`.

`DATABASE_URL` on **callloop-prodwork** is an Environment variable (not a Secret File).

New workspace: Dashboard → **New → Blueprint** and point at this repo’s `render.yaml`, or recreate a Web Service with the table above. Paste secrets in the dashboard (`sync: false` in the Blueprint).

**Not in this AC:** SQLite on a mounted disk (free instances have no persistent volume). Uploaded calls are lost on sleep/redeploy until a paid disk is attached.

---

## Smoke checklist

1. UI loads at http://127.0.0.1:5173 — **Log in** (or sign up) before the workspace
2. Status chip shows **SANDBOX** or **LIVE**  
3. Upload a short call (or use Agents Pulse / call list if data exists)  
4. Open a scorecard after audit completes  
5. **Integrations** lists JustCall-sourced evaluations (after keys + sync)  
6. Logs: `logs/callproof.log`  
7. Expect **Training** to say Coming soon — that is normal

---

## Useful paths

| Path | Purpose |
|------|---------|
| `.env` | Secrets (local only) |
| `frontend/.env` | Optional `VITE_API_URL`; `VITE_SUPABASE_URL` + `VITE_SUPABASE_ANON_KEY` for login |
| `logs/callproof.log` | Backend event log |
| `callproof.db` | Calls, segments, audits, usage |
| `audio/` | Playback copies |
| `rubric.json` | Runtime scoring rubric (v8 shape) |
| `backend/` | Python API package |

---

## Security notes

- Do not commit API keys or `.env`.
- Sandbox keys are for bootstrap; treat live keys as secrets.
- Cost figures in the UI are **estimates** from local usage × `COST_*` rates, not provider invoices.
- HTTP **5xx** and unhandled API crashes can notify via macOS Notification Center (default on this Mac), optional `ERROR_NOTIFY_WEBHOOK_URL`, and default operator email (Mail.app). Notices are redacted; 4xx is not alerted.

### Error notifications (local)

On API **500+**, you get a macOS banner: **CallProof API error**. Restart uvicorn after pulling this change.

Optional in `.env`:

```bash
ERROR_NOTIFY_DESKTOP=true
ERROR_NOTIFY_WEBHOOK_URL=
ERROR_NOTIFY_MIN_INTERVAL_SECONDS=60
```

Error emails go to the two operator addresses baked into the app. Mail.app sends them if an account is signed in. Allow **Mail** control if macOS asks. Set `ERROR_NOTIFY_EMAIL=off` to disable, or add extra addresses as a comma-separated list. SMTP is only used if `SMTP_HOST` is set.

---

## License / contact

Internal CallProof / Call Loop project. For PyAI keys and Anthropic access, use your team's console accounts.
