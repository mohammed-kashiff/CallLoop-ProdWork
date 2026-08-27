# ADR 001: Tenancy model

**Status:** Accepted, in effect since the CL-4 → CL-25 migration.

## Context

CallLoop started as a single-tenant hackathon build: one shared SQLite file
on local disk, no concept of separate customers. Turning it into a real
product meant deciding how multiple companies' data would be kept apart, and
how a company's employees would end up grouped together in the first place.

Two constraints shaped every decision below:
- The team is small and the app runs on Render's free tier — a
  schema-per-tenant or database-per-tenant model would have meant real
  operational overhead (per-tenant migrations, connection management,
  provisioning) with no corresponding benefit at this scale.
- Postgres (via Supabase) was already the target for Auth and Storage, so
  Postgres's own isolation primitive — Row Level Security — was available
  for free rather than something to build.

## Decision

**Shared schema, `org_id` on every tenant table, RLS as a second layer under
the application code — not instead of it.**

1. **Every tenant table carries `org_id UUID NOT NULL REFERENCES orgs(id)`
   from its very first migration** (`0001_orgs_calls_segments_audits.py`) —
   `calls`, `segments`, `audits`, `rubrics`, `api_usage`, `org_credentials`.
   There was never a version of the schema without this column; it was not
   retrofitted.

2. **The app layer resolves `org_id` exclusively from the verified JWT**
   (`backend/org_ids.py`'s `contextvars`-based `bind_org_id`/`bound_org_id`),
   never from a query param, path segment, or request body. This is the
   first isolation layer — a client cannot ask for another org's data by
   forging an id in the request.

3. **Postgres RLS re-checks the same thing independently**, as a second
   layer that holds even if the app layer has a bug. This required a
   dedicated database role: `callproof_app` (`NOLOGIN NOSUPERUSER
   NOBYPASSRLS`, added in `0005_rls.py`), which the API switches to via
   `SET LOCAL ROLE` on every connection (`backend/db.py::apply_tenant_gucs`).
   `DATABASE_URL` still connects as `postgres` (which *does* bypass RLS —
   Alembic needs that to run DDL), so the switch to `callproof_app` at
   connection time is what actually makes RLS bite for API traffic. Getting
   this role distinction right mattered in practice: an earlier iteration
   ran the API directly as `postgres` and RLS policies existed but enforced
   nothing, because `postgres` bypasses RLS unconditionally regardless of
   policy content.

4. **`org_members` is the one deliberate exception — RLS is disabled on it**
   (`0007_org_members_no_rls.py`). A signup has no `org_id` yet at the moment
   it needs to find (or create) its own membership row, so RLS on the table
   that assigns tenancy in the first place would be a chicken-and-egg
   deadlock. Isolation for this table comes entirely from the app layer:
   `ensure_membership()` only ever looks up a row by the `user_id` taken
   from the verified JWT, never by a client-supplied org id. **Do not
   "fix" this by re-enabling RLS on `org_members`** — Supabase's platform
   auto-enabled RLS on it once already, with zero policies, and it broke
   every login with a deny-all `InsufficientPrivilege` error until it was
   found and explicitly disabled again.

## How a company's employees end up in the same org

This was originally unimplemented — every new signup got its own brand-new
org regardless of email domain, including two people from the same company.
`ensure_membership()` (`backend/auth.py`) now does this instead, as of CL-23:

- On signup, the email's domain is extracted. If it's a public mailbox
  provider (`gmail.com`, `outlook.com`, `yahoo.com`, and similar — see the
  `_PUBLIC_EMAIL_DOMAINS` blocklist), the signup always gets its own new org,
  same as before. **This blocklist is not optional** — without it, every
  Gmail signup on the platform would auto-join the same org as every other
  Gmail user who ever signed up, which is a real cross-tenant leak, not a
  hypothetical one.
- Otherwise, the domain is looked up against `orgs.domain` (added in
  `0010_orgs_domain_column.py`, nullable + unique). First signup from a new
  domain creates the org (`domain` and `name` both set to the domain,
  signer becomes `owner`); a later signup from the same domain joins that
  org as `member`.
- The domain-collision race (two signups for a brand-new domain landing at
  the same instant) is resolved via `INSERT ... ON CONFLICT (domain) DO
  NOTHING RETURNING id`, and the losing request resolves the winner's org id
  through `public.org_id_for_domain(text)` — a narrow `SECURITY DEFINER` SQL
  function that returns only an `id` for an exact domain match. This exists
  specifically so the app layer never has to open an RLS-bypassing
  connection to answer "who already owns this domain" — that was the first
  draft of this fix, and it failed a dedicated test
  (`test_api_handlers_do_not_bypass_rls`) that exists to keep `bypass_rls`
  out of every request-handling code path. The function is the fix that
  keeps that invariant intact.

**Known consequence, not yet addressed:** any signup that can complete
Supabase's email verification for a matched domain becomes a `member` of
that org automatically — there is no invite or owner-approval step. This is
acceptable for a small, trusted team signing up together, but should be
revisited before this becomes self-serve for larger organizations where not
everyone who can receive mail at the domain should see the company's call
data.

## `DEFAULT_ORG_ID` — what it is and isn't

`DEFAULT_ORG_ID` (`00000000-0000-4000-8000-000000000001`) is a placeholder
org seeded once by migration `0001`. **It is no longer reachable by human
signup** — CL-23 removed the branch that used to assign it to whichever user
happened to sign up first. It still exists to give non-request-scoped
background work somewhere to write: the JustCall webhook's host-fallback
path, QA scoring's fallback when no org is bound, and usage tracking's
fallback. `ensure_placeholder_org()` (`backend/auth.py`) idempotently
re-seeds this one specific row if it's ever missing (e.g., after a full data
wipe) — called from those three fallback paths and from API startup, never
from `ensure_membership()`.

## Alternatives considered

- **Schema-per-tenant.** Rejected — real per-tenant migration and connection
  overhead for no isolation benefit RLS doesn't already provide at this
  scale, and Render's free tier plus a small team make that overhead costly
  relative to the benefit.
- **Database-per-tenant.** Rejected for the same reason, more so — Supabase
  project-per-tenant would also multiply cost and operational surface
  (Auth, Storage, Vault, migrations, all ×N) for a product that doesn't yet
  have enough tenants to justify it.

## Consequences

- Adding a new tenant table means remembering `org_id NOT NULL REFERENCES
  orgs(id)` **and** a matching RLS policy in the same migration — RLS is
  additive per table, not automatic. `tests/test_rls.py` enforces the
  policy shape; there is no equivalent automatic check that a brand-new
  table hasn't been forgotten entirely.
- `bypass_rls=True` (`backend/db.py`) exists for a short, explicit list of
  non-tenant-table admin/background uses (Vault catalog, audio backfill,
  the JustCall credential poller). `tests/test_rls.py::test_api_handlers_do_not_bypass_rls`
  keeps it out of `api.py`, `auth.py`, `transcribe.py`, `qa_engine.py`,
  `audit_store.py`, and `pyai_usage.py` by name — extend that list if a new
  file starts touching tenant data in a request-handling path.
