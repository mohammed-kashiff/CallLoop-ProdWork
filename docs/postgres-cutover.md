# Postgres-only runtime

The API talks to **Postgres** only (`DATABASE_URL` or `SUPABASE_DB_URL`). There is no SQLite fallback, no `callproof.db` path in the process, and no Render disk for a database file.

Calls, scorecards, and usage live in Postgres. Recordings live in private Storage (`call-audio`). Schema changes go through Alembic (`alembic upgrade head` on Render Pre-Deploy).

## Local development

1. Copy `.env.example` → `.env` (gitignored). Set `DATABASE_URL` to a Supabase session-pooler URI or a local Postgres URI. Never commit the value.
2. Apply schema before the first `uvicorn` start:

```bash
unset DATABASE_URL
source .venv/bin/activate
alembic upgrade head
uvicorn backend.api:app --reload --port 8000
```

`unset DATABASE_URL` makes the gitignored `.env` win over a stale shell export. Tests that need a live database skip when the URL is missing (CI always sets it).

You do **not** need a local `callproof.db` file. If one exists from an old checkout, it is unused; you can delete it.

Full install steps: [README.md](../README.md) (Install & run).

## Host config (Render)

`render.yaml` has **no** `disks:` key. Do not add a persistent volume for SQLite or local files.

Dashboard check on **callloop-prodwork**: Settings must not list a disk mounted for `callproof.db`. `DATABASE_URL` is an Environment variable (not a Secret File). Pre-Deploy Command is `alembic upgrade head`.

## Rollback (write this down before a deploy)

Rollback is **not** “put SQLite back.” That path is gone. Use one of these, in this order of safety:

### 1. App rollback (usual)

Redeploy the previous git SHA on **callloop-prodwork** (Render → Manual Deploy → a known-good commit, or revert the PR and push). Leave `DATABASE_URL` unchanged. The previous SHA also expects Postgres.

### 2. Data rollback (Supabase)

If the deploy wrote bad rows but the schema is still at head:

1. Pause deploys on Render (so the API stops writing).
2. Restore from a Supabase backup or Point-in-Time Recovery in the dashboard. Do not paste connection strings into chat or git.
3. Confirm `alembic_version` matches the restored schema (`alembic current` from a laptop with `.env`, never from a log dump of the URI).
4. Resume deploys.

### 3. Schema rollback (clone only)

`alembic downgrade` is **destructive** (drops tables/policies). **Do not run it against production.**

Practise on a **clone** (CI Postgres, or a throwaway Supabase branch/project):

```bash
# CI / clone DATABASE_URL only — never production
alembic current
alembic downgrade -1
alembic upgrade head
alembic current
```

CI runs that cycle on every push (`.github/workflows/ci.yml`: upgrade → `downgrade -1` → upgrade → pytest). That is the tested rollback of the latest revision. A full `downgrade base` is not the production procedure.

If production schema must go back one revision: restore a backup from **before** the migration instead of `downgrade` on the live database.

## Cutover checklist

- [ ] `DATABASE_URL` set on Render (session pooler preferred).
- [ ] `render.yaml` has no `disks:`.
- [ ] Dashboard has no leftover SQLite volume.
- [ ] Pre-Deploy is `alembic upgrade head`.
- [ ] `GET /health` returns 200 after deploy.
- [ ] App rollback SHA is known (previous green deploy).
- [ ] Supabase backup / PITR is enabled before the deploy.
