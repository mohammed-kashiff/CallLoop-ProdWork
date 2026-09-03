import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { apiFetch, fmtUsd, readError } from '../lib/api'
import { adminFlagOn, TRIAL_FLAGS, type FeatureMap } from '../lib/features'
import { formatBytes } from '../lib/format'
import { supabase, supabaseConfigured } from '../lib/supabase'
import { useAuth } from '../context/AuthContext'
import { isAdminHost } from '../lib/adminHost'

type ProvisionResult = {
  email: string
  org_name: string
  created: boolean
  temporary_password: string
}

type DirectoryRow = {
  user_id: string
  email: string | null
  first_name: string | null
  last_name: string | null
  role: string | null
  org_id: string
  org_name: string | null
  short_id: number | null
  first_seen: string | null
  last_sign_in_at: string | null
}

type UsagePayload = {
  org_id: string
  usage: {
    total_hits?: number
    total_polls?: number
    by_provider?: Record<
      string,
      { hits?: number; actions?: number; polls?: number; units?: number }
    >
  }
  cost: { pyai_usd: number; claude_usd: number; total_usd: number }
  features: FeatureMap
}

type OrgCallRow = {
  call_id: number
  filename: string | null
  created_at: string | null
  audio_seconds: number | null
  mode: 'pyai' | 'selfhosted'
  audited: boolean
  uploaded_by: string | null
  requested_by: string | null
  deleted: boolean
  deleted_at: string | null
  deleted_by_short_id: number | null
  data_size_bytes: number
}

type OrgDetailPayload = {
  org_id: string
  total_calls: number
  audited_count: number
  calls: OrgCallRow[]
  calls_truncated: boolean
  total_data_size_bytes: number
}

type RubricPayload = {
  org_id: string
  source: 'custom' | 'legacy'
  rubric_id: string | null
  version: number | null
  updated_at: string | null
  weights: Record<string, number>
}

const RUBRIC_DIMENSIONS: { id: string; label: string }[] = [
  { id: 'resolution_effectiveness', label: 'Resolution Effectiveness' },
  { id: 'ownership_next_steps', label: 'Ownership & Next Steps' },
  { id: 'active_listening', label: 'Active Listening' },
  { id: 'tone_empathy_professionalism', label: 'Tone, Empathy & Professionalism' },
]

type ActivityEvent = {
  at: string | null
  kind: 'upload' | 'audit' | 'flag_change' | 'delete'
  actor: string | null
  call_id: number | null
  filename: string | null
  feature_key: string | null
  enabled: boolean | null
}

type ActivityPayload = {
  org_id: string
  events: ActivityEvent[]
  truncated: boolean
}

type PasswordEvent = {
  event_type: 'self_service' | 'admin_reset_email' | 'admin_direct_reset'
  actor_email: string | null
  ip_address: string | null
  created_at: string | null
}

function passwordEventLabel(e: PasswordEvent): string {
  if (e.event_type === 'self_service') return 'Self-service'
  if (e.event_type === 'admin_reset_email') return `Admin reset email${e.actor_email ? ` (${e.actor_email})` : ''}`
  return `Admin direct reset${e.actor_email ? ` (${e.actor_email})` : ''}`
}

function ymd(d: Date): string {
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function weekAgo(): string {
  const d = new Date()
  d.setDate(d.getDate() - 7)
  return ymd(d)
}

function activityLabel(e: ActivityEvent): string {
  if (e.kind === 'upload') return e.filename || (e.call_id != null ? `Call ${e.call_id}` : 'Upload')
  if (e.kind === 'audit') return e.call_id != null ? `Call ${e.call_id}` : 'Audit'
  if (e.kind === 'delete') return e.filename || (e.call_id != null ? `Call ${e.call_id}` : 'Delete')
  const flag = e.feature_key || 'flag'
  if (e.enabled === true) return `${flag} on`
  if (e.enabled === false) return `${flag} off`
  return flag
}

function activityKind(kind: ActivityEvent['kind']): string {
  if (kind === 'upload') return 'Upload'
  if (kind === 'audit') return 'Audit'
  if (kind === 'delete') return 'Delete'
  return 'Flag'
}

function displayName(row: DirectoryRow): string {
  const n = [row.first_name, row.last_name].filter(Boolean).join(' ').trim()
  return n || '—'
}

export function Admin() {
  const { isPlatformAdmin } = useAuth()
  const [q, setQ] = useState('')
  const [rows, setRows] = useState<DirectoryRow[]>([])
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<DirectoryRow | null>(null)
  const [usage, setUsage] = useState<UsagePayload | null>(null)
  const [orgDetail, setOrgDetail] = useState<OrgDetailPayload | null>(null)
  const [busy, setBusy] = useState(false)
  const [actOrg, setActOrg] = useState('')
  const [actSince, setActSince] = useState(weekAgo)
  const [actUntil, setActUntil] = useState(() => ymd(new Date()))
  const [activity, setActivity] = useState<ActivityPayload | null>(null)
  const [actBusy, setActBusy] = useState(false)
  const [actError, setActError] = useState<string | null>(null)

  const [pEmail, setPEmail] = useState('')
  const [pFirst, setPFirst] = useState('')
  const [pLast, setPLast] = useState('')
  const [pOrgName, setPOrgName] = useState('')
  const [provisioning, setProvisioning] = useState(false)
  const [provisionError, setProvisionError] = useState<string | null>(null)
  const [provisionResult, setProvisionResult] = useState<ProvisionResult | null>(null)
  const [copied, setCopied] = useState(false)
  const [resetEmailBusy, setResetEmailBusy] = useState(false)
  const [resetEmailInfo, setResetEmailInfo] = useState<string | null>(null)
  const [resetEmailError, setResetEmailError] = useState<string | null>(null)
  const [pwEvents, setPwEvents] = useState<PasswordEvent[] | null>(null)

  const [rubric, setRubric] = useState<RubricPayload | null>(null)
  const [rubricDraft, setRubricDraft] = useState<Record<string, number>>({})
  const [rubricSaving, setRubricSaving] = useState(false)
  const [rubricError, setRubricError] = useState<string | null>(null)
  const [rubricSaveInfo, setRubricSaveInfo] = useState<string | null>(null)

  const search = useCallback(async (needle: string) => {
    const r = await apiFetch(`/api/admin/directory?q=${encodeURIComponent(needle)}`)
    if (!r.ok) throw new Error(await readError(r, 'Could not search the directory.'))
    const data = (await r.json()) as { rows?: DirectoryRow[] }
    setRows(Array.isArray(data.rows) ? data.rows : [])
  }, [])

  useEffect(() => {
    if (!isPlatformAdmin) return
    const t = window.setTimeout(() => {
      search(q).catch((e: unknown) =>
        setError(e instanceof Error ? e.message : 'Could not search the directory.'),
      )
    }, 250)
    return () => window.clearTimeout(t)
  }, [q, isPlatformAdmin, search])

  const provisionUser = async (e: FormEvent) => {
    e.preventDefault()
    setProvisionError(null)
    setProvisionResult(null)
    setCopied(false)
    setProvisioning(true)
    try {
      const r = await apiFetch('/api/admin/provision-user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: pEmail.trim(),
          first_name: pFirst.trim(),
          last_name: pLast.trim(),
          org_mode: 'new',
          org_name: pOrgName.trim(),
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not provision that user.'))
      const data = (await r.json()) as ProvisionResult
      setProvisionResult(data)
      setPEmail('')
      setPFirst('')
      setPLast('')
      setPOrgName('')
      search(q).catch(() => {})
    } catch (err: unknown) {
      setProvisionError(err instanceof Error ? err.message : 'Could not provision that user.')
    } finally {
      setProvisioning(false)
    }
  }

  const copyPassword = async () => {
    if (!provisionResult) return
    try {
      await navigator.clipboard.writeText(provisionResult.temporary_password)
      setCopied(true)
    } catch {
      setCopied(false)
    }
  }

  const loadPasswordEvents = async (userId: string) => {
    try {
      const r = await apiFetch(`/api/admin/users/${encodeURIComponent(userId)}/password-events`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load password history.'))
      const data = (await r.json()) as { events?: PasswordEvent[] }
      setPwEvents(Array.isArray(data.events) ? data.events : [])
    } catch {
      // Secondary detail on the panel — a miss here must not block usage/detail.
      setPwEvents(null)
    }
  }

  const loadOrg = async (row: DirectoryRow) => {
    setSelected(row)
    setError(null)
    setResetEmailInfo(null)
    setResetEmailError(null)
    setActOrg(row.org_id)
    setPwEvents(null)
    setRubric(null)
    setRubricError(null)
    setRubricSaveInfo(null)
    setBusy(true)
    try {
      const [usageRes, detailRes, rubricRes] = await Promise.all([
        apiFetch(`/api/admin/usage?org_id=${encodeURIComponent(row.org_id)}`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/detail`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/rubric`),
      ])
      if (!usageRes.ok) throw new Error(await readError(usageRes, 'Could not load usage.'))
      if (!detailRes.ok) throw new Error(await readError(detailRes, 'Could not load org detail.'))
      setUsage((await usageRes.json()) as UsagePayload)
      setOrgDetail((await detailRes.json()) as OrgDetailPayload)
      if (rubricRes.ok) {
        const data = (await rubricRes.json()) as RubricPayload
        setRubric(data)
        setRubricDraft(data.weights)
      } else {
        setRubricError(await readError(rubricRes, 'Could not load the rubric.'))
      }
    } catch (e: unknown) {
      setUsage(null)
      setOrgDetail(null)
      setError(e instanceof Error ? e.message : 'Could not load usage.')
    } finally {
      setBusy(false)
    }
    void loadPasswordEvents(row.user_id)
  }

  const rubricTotal = RUBRIC_DIMENSIONS.reduce(
    (sum, dim) => sum + (rubricDraft[dim.id] ?? 0),
    0,
  )

  const saveRubric = async () => {
    if (!selected || rubricTotal !== 100) return
    setRubricSaving(true)
    setRubricError(null)
    setRubricSaveInfo(null)
    try {
      const r = await apiFetch(`/api/admin/orgs/${encodeURIComponent(selected.org_id)}/rubric`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ weights: rubricDraft }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save the rubric.'))
      const data = (await r.json()) as RubricPayload
      setRubric(data)
      setRubricDraft(data.weights)
      setRubricSaveInfo(`Saved — version ${data.version} active.`)
    } catch (e: unknown) {
      setRubricError(e instanceof Error ? e.message : 'Could not save the rubric.')
    } finally {
      setRubricSaving(false)
    }
  }

  const loadActivity = async (e: FormEvent) => {
    e.preventDefault()
    setActError(null)
    setActBusy(true)
    try {
      const ref = actOrg.trim()
      const params = new URLSearchParams({ since: actSince, until: actUntil })
      if (/^\d+$/.test(ref)) params.set('short_id', ref)
      else params.set('org_id', ref)
      const r = await apiFetch(`/api/admin/activity?${params.toString()}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load activity.'))
      setActivity((await r.json()) as ActivityPayload)
    } catch (err: unknown) {
      setActivity(null)
      setActError(err instanceof Error ? err.message : 'Could not load activity.')
    } finally {
      setActBusy(false)
    }
  }

  const toggle = async (key: string, enabled: boolean) => {
    if (!selected) return
    setError(null)
    const r = await apiFetch('/api/admin/features', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        org_id: selected.org_id,
        feature_key: key,
        enabled,
      }),
    })
    if (!r.ok) {
      setError(await readError(r, 'Could not update that flag.'))
      return
    }
    const data = (await r.json()) as { features?: FeatureMap }
    setUsage((prev) =>
      prev ? { ...prev, features: data.features || prev.features } : prev,
    )
  }

  const sendResetEmail = async () => {
    if (!selected?.email) return
    setResetEmailError(null)
    setResetEmailInfo(null)
    if (!supabase) {
      setResetEmailError('Auth is not configured.')
      return
    }
    setResetEmailBusy(true)
    try {
      const { error: err } = await supabase.auth.resetPasswordForEmail(selected.email.trim(), {
        redirectTo: `${window.location.origin}/reset-password`,
      })
      if (err) {
        const msg = err.message.toLowerCase()
        const leaky = /not found|does not exist|no user|unregistered|could not find/.test(
          msg,
        )
        if (!leaky) {
          setResetEmailError(err.message)
          return
        }
      }
      setResetEmailInfo(
        'Reset email sent. They set the new password from the link — you will not see it.',
      )
      try {
        await apiFetch('/api/admin/log-password-reset-request', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            user_id: selected.user_id,
            email: selected.email,
          }),
        })
      } catch {
        /* email already sent; a log miss must not look like a failed reset */
      }
      void loadPasswordEvents(selected.user_id)
    } catch {
      setResetEmailError('Could not send the reset email.')
    } finally {
      setResetEmailBusy(false)
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Platform</p>
              <h1>Admin</h1>
            </div>
          </header>
          <p className="admin-provision-hint">
            This console is limited to platform admins.
          </p>
        </>
      )
    }
    return <Navigate to="/" replace />
  }

  const pyai = usage?.usage?.by_provider?.pyai
  const claude = usage?.usage?.by_provider?.anthropic

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Platform</p>
          <h1>Admin</h1>
        </div>
      </header>

      <section className="admin-provision">
        <h2>Provision user</h2>
        <p className="admin-provision-hint">
          Creates a login and a new org, named as you choose. The password is
          generated and shown once here — copy it and share it with the
          person alongside their email.
        </p>
        <form className="admin-provision-form" onSubmit={(e) => void provisionUser(e)}>
          <label>
            Email
            <input
              type="email"
              value={pEmail}
              onChange={(e) => setPEmail(e.target.value)}
              required
            />
          </label>
          <label>
            First name
            <input
              type="text"
              value={pFirst}
              onChange={(e) => setPFirst(e.target.value)}
              required
            />
          </label>
          <label>
            Last name
            <input
              type="text"
              value={pLast}
              onChange={(e) => setPLast(e.target.value)}
              required
            />
          </label>
          <label>
            Org name
            <input
              type="text"
              value={pOrgName}
              onChange={(e) => setPOrgName(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="start-btn" disabled={provisioning}>
            {provisioning ? 'Creating…' : 'Create'}
          </button>
        </form>

        {provisionError ? (
          <p className="upload-error" role="alert">
            {provisionError}
          </p>
        ) : null}

        {provisionResult ? (
          <div className="admin-provision-result" role="status">
            <p>
              <strong>{provisionResult.email}</strong>{' '}
              {provisionResult.created ? 'created' : 'added to the existing org'} in{' '}
              <strong>{provisionResult.org_name}</strong>.
            </p>
            <p className="admin-provision-warning">
              This password is shown once — copy it now.
            </p>
            <div className="admin-provision-secret">
              <code>{provisionResult.temporary_password}</code>
              <button type="button" className="ghost-btn" onClick={() => void copyPassword()}>
                {copied ? 'Copied' : 'Copy'}
              </button>
            </div>
          </div>
        ) : null}
      </section>

      <section className="admin-activity">
        <h2>Activity</h2>
        <p className="admin-provision-hint">
          Uploads, audits, and flag changes for one org in a date range.
          Retranscribes show as a new audit on that call. Not application logs.
        </p>
        <form className="admin-provision-form" onSubmit={(e) => void loadActivity(e)}>
          <label>
            Org id or short id
            <input
              type="text"
              value={actOrg}
              onChange={(e) => setActOrg(e.target.value)}
              placeholder="UUID or 100001"
              required
            />
          </label>
          <label>
            From
            <input
              type="date"
              value={actSince}
              onChange={(e) => setActSince(e.target.value)}
              required
            />
          </label>
          <label>
            To
            <input
              type="date"
              value={actUntil}
              onChange={(e) => setActUntil(e.target.value)}
              required
            />
          </label>
          <button type="submit" className="start-btn" disabled={actBusy}>
            {actBusy ? 'Loading…' : 'Load'}
          </button>
        </form>
        {actError ? (
          <p className="upload-error" role="alert">
            {actError}
          </p>
        ) : null}
        {activity ? (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Type</th>
                  <th>Who</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {activity.events.map((ev, i) => (
                  <tr key={`${ev.kind}-${ev.at}-${ev.call_id ?? ev.feature_key ?? i}`}>
                    <td>{ev.at ? new Date(ev.at).toLocaleString() : '—'}</td>
                    <td>{activityKind(ev.kind)}</td>
                    <td>{ev.actor || '—'}</td>
                    <td>{activityLabel(ev)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {activity.events.length === 0 ? (
              <p className="empty-copy">No activity in that window.</p>
            ) : null}
            {activity.truncated ? (
              <p className="admin-provision-hint">Showing the most recent rows in this range.</p>
            ) : null}
          </div>
        ) : null}
      </section>

      <label className="admin-search">
        Search directory
        <input
          type="search"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Email, name, org id, user id, or short id"
        />
      </label>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="admin-layout">
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Org</th>
                <th>Short id</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={`${row.user_id}-${row.org_id}`}
                  className={
                    selected?.user_id === row.user_id && selected?.org_id === row.org_id
                      ? 'is-selected'
                      : ''
                  }
                >
                  <td>
                    <button type="button" onClick={() => void loadOrg(row)}>
                      {row.email || '—'}
                    </button>
                  </td>
                  <td>{displayName(row)}</td>
                  <td>
                    <span className="admin-org">{row.org_name || '—'}</span>
                    <span className="admin-id">{row.org_id}</span>
                  </td>
                  <td>{row.short_id ?? '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {rows.length === 0 ? <p className="empty-copy">No matches.</p> : null}
        </div>

        <div className="admin-detail">
          {!selected ? (
            <p className="empty-copy">Select a row to see usage, cost, and flags.</p>
          ) : (
            <>
              <div className="admin-card">
                <h2>{selected.org_name || 'Organization'}</h2>
                <p className="admin-id">{selected.org_id}</p>
                {busy ? <p className="empty-copy">Loading…</p> : null}
                {usage ? (
                  <dl className="admin-stats">
                    <div>
                      <dt>PyAI calls</dt>
                      <dd>{pyai?.hits ?? 0}</dd>
                    </div>
                    <div>
                      <dt>PyAI polls</dt>
                      <dd>{pyai?.polls ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Anthropic calls</dt>
                      <dd>{claude?.hits ?? 0}</dd>
                    </div>
                    <div>
                      <dt>Est. spend</dt>
                      <dd>{fmtUsd(usage.cost.total_usd)}</dd>
                    </div>
                    <div>
                      <dt>PyAI</dt>
                      <dd>{fmtUsd(usage.cost.pyai_usd)}</dd>
                    </div>
                    <div>
                      <dt>Claude</dt>
                      <dd>{fmtUsd(usage.cost.claude_usd)}</dd>
                    </div>
                  </dl>
                ) : null}
                {orgDetail ? (
                  <dl className="admin-stats">
                    <div>
                      <dt>Total calls</dt>
                      <dd>{orgDetail.total_calls}</dd>
                    </div>
                    <div>
                      <dt>Audited</dt>
                      <dd>{orgDetail.audited_count}</dd>
                    </div>
                    <div>
                      <dt>Data stored</dt>
                      <dd>{formatBytes(orgDetail.total_data_size_bytes)}</dd>
                    </div>
                  </dl>
                ) : null}
                <p className="admin-provision-hint">
                  Per-call detail moved to <Link to="/call-logs">Call logs</Link>.
                </p>
              </div>

              <div className="admin-card">
                <h3>Feature flags</h3>
                <ul className="admin-flags">
                  {TRIAL_FLAGS.map((flag) => {
                    const on = adminFlagOn(usage?.features, flag)
                    return (
                      <li key={flag.key}>
                        <div className="admin-flag-row">
                          <span className="admin-flag-label">{flag.label}</span>
                          <span className="toggle-switch">
                            <input
                              type="checkbox"
                              checked={on}
                              disabled={!usage || busy}
                              onChange={(e) => void toggle(flag.key, e.target.checked)}
                              aria-label={flag.label}
                            />
                            <span className="toggle-track" />
                            <span className="toggle-thumb" />
                          </span>
                        </div>
                        {flag.description ? (
                          <p className="admin-provision-hint">{flag.description}</p>
                        ) : null}
                      </li>
                    )
                  })}
                </ul>
              </div>

              <div className="admin-card">
                <h3>Rubric</h3>
                <p className="admin-org">{selected.org_name || 'Organization'}</p>
                <p className="admin-id">{selected.org_id}</p>
                {rubricError ? (
                  <p className="upload-error" role="alert">
                    {rubricError}
                  </p>
                ) : null}
                {rubric ? (
                  <>
                    <p className="admin-provision-hint">
                      {rubric.source === 'custom'
                        ? `Custom — version ${rubric.version}, updated ${
                            rubric.updated_at ? new Date(rubric.updated_at).toLocaleString() : '—'
                          }.`
                        : 'Not yet customized — showing default weights.'}
                    </p>
                    <div className="admin-rubric-grid">
                      {RUBRIC_DIMENSIONS.map((dim) => (
                        <label key={dim.id} className="admin-rubric-field">
                          <span>{dim.label}</span>
                          <input
                            type="number"
                            min={0}
                            max={100}
                            value={rubricDraft[dim.id] ?? 0}
                            disabled={rubricSaving}
                            onChange={(e) =>
                              setRubricDraft((prev) => ({
                                ...prev,
                                [dim.id]: Number(e.target.value) || 0,
                              }))
                            }
                          />
                        </label>
                      ))}
                    </div>
                    <p
                      className={
                        rubricTotal === 100 ? 'admin-rubric-total' : 'admin-rubric-total is-off'
                      }
                    >
                      Total: {rubricTotal} / 100
                    </p>
                    <button
                      type="button"
                      className="start-btn"
                      disabled={rubricTotal !== 100 || rubricSaving}
                      onClick={() => void saveRubric()}
                    >
                      {rubricSaving ? 'Saving…' : 'Save'}
                    </button>
                    {rubricSaveInfo ? (
                      <p className="auth-info" role="status">
                        {rubricSaveInfo}
                      </p>
                    ) : null}
                  </>
                ) : !rubricError ? (
                  <p className="empty-copy">Loading…</p>
                ) : null}
              </div>

              <div className="admin-card admin-support">
                <h3>Account recovery</h3>
                <p className="admin-id">{selected.email || 'No email on this row'}</p>
                <p className="admin-provision-hint">
                  Sends the same reset link as Forgot password. You never see
                  or set the new password — they finish it from the email.
                </p>
                <div className="admin-support-actions">
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={
                      resetEmailBusy || !selected.email || !supabaseConfigured
                    }
                    onClick={() => void sendResetEmail()}
                  >
                    {resetEmailBusy ? 'Sending…' : 'Send reset email'}
                  </button>
                </div>
                {resetEmailError ? (
                  <p className="upload-error" role="alert">
                    {resetEmailError}
                  </p>
                ) : null}
                {resetEmailInfo ? (
                  <p className="auth-info" role="status">
                    {resetEmailInfo}
                  </p>
                ) : null}
                {pwEvents && pwEvents.length > 0 ? (
                  <div className="admin-table-wrap">
                    <table className="admin-table">
                      <thead>
                        <tr>
                          <th>When</th>
                          <th>Event</th>
                          <th>IP</th>
                        </tr>
                      </thead>
                      <tbody>
                        {pwEvents.map((e, i) => (
                          <tr key={i}>
                            <td>{e.created_at ? new Date(e.created_at).toLocaleString() : '—'}</td>
                            <td>{passwordEventLabel(e)}</td>
                            <td>{e.ip_address || '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ) : pwEvents && pwEvents.length === 0 ? (
                  <p className="empty-copy">No password changes recorded.</p>
                ) : null}
              </div>
            </>
          )}
        </div>
      </div>
    </>
  )
}
