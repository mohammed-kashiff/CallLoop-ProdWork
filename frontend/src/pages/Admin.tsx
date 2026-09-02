import { useCallback, useEffect, useState, type FormEvent } from 'react'
import { Navigate } from 'react-router-dom'
import { apiFetch, fmtUsd, readError } from '../lib/api'
import { adminFlagOn, TRIAL_FLAGS, type FeatureMap } from '../lib/features'
import { formatTime } from '../lib/format'
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
}

type OrgDetailPayload = {
  org_id: string
  total_calls: number
  audited_count: number
  calls: OrgCallRow[]
  calls_truncated: boolean
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

  const loadOrg = async (row: DirectoryRow) => {
    setSelected(row)
    setError(null)
    setResetEmailInfo(null)
    setResetEmailError(null)
    setBusy(true)
    try {
      const [usageRes, detailRes] = await Promise.all([
        apiFetch(`/api/admin/usage?org_id=${encodeURIComponent(row.org_id)}`),
        apiFetch(`/api/admin/orgs/${encodeURIComponent(row.org_id)}/detail`),
      ])
      if (!usageRes.ok) throw new Error(await readError(usageRes, 'Could not load usage.'))
      if (!detailRes.ok) throw new Error(await readError(detailRes, 'Could not load org detail.'))
      setUsage((await usageRes.json()) as UsagePayload)
      setOrgDetail((await detailRes.json()) as OrgDetailPayload)
    } catch (e: unknown) {
      setUsage(null)
      setOrgDetail(null)
      setError(e instanceof Error ? e.message : 'Could not load usage.')
    } finally {
      setBusy(false)
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

        <aside className="admin-panel">
          {!selected ? (
            <p className="empty-copy">Select a row to see usage, cost, and flags.</p>
          ) : (
            <>
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
              <ul className="admin-flags">
                {TRIAL_FLAGS.map((flag) => {
                  const on = adminFlagOn(usage?.features, flag)
                  return (
                    <li key={flag.key}>
                      <label>
                        <input
                          type="checkbox"
                          checked={on}
                          disabled={!usage || busy}
                          onChange={(e) => void toggle(flag.key, e.target.checked)}
                        />
                        {flag.label}
                      </label>
                      {flag.description ? (
                        <p className="admin-provision-hint">{flag.description}</p>
                      ) : null}
                    </li>
                  )
                })}
              </ul>
              <div className="admin-calls">
                <h3>Calls</h3>
                {orgDetail ? (
                  <>
                    <dl className="admin-stats">
                      <div>
                        <dt>Total calls</dt>
                        <dd>{orgDetail.total_calls}</dd>
                      </div>
                      <div>
                        <dt>Audited</dt>
                        <dd>{orgDetail.audited_count}</dd>
                      </div>
                    </dl>
                    <div className="admin-table-wrap">
                      <table className="admin-table">
                        <thead>
                          <tr>
                            <th>Date</th>
                            <th>Filename</th>
                            <th>Length</th>
                            <th>Engine</th>
                            <th>Audited</th>
                            <th>Uploaded by</th>
                            <th>Requested by</th>
                          </tr>
                        </thead>
                        <tbody>
                          {orgDetail.calls.map((c) => (
                            <tr key={c.call_id}>
                              <td>
                                {c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}
                              </td>
                              <td>{c.filename || '—'}</td>
                              <td>
                                {c.audio_seconds != null ? formatTime(c.audio_seconds) : '—'}
                              </td>
                              <td>{c.mode === 'selfhosted' ? 'Self-hosted' : 'PyAI'}</td>
                              <td>{c.audited ? 'Yes' : 'No'}</td>
                              <td>{c.uploaded_by || '—'}</td>
                              <td>{c.requested_by || '—'}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                      {orgDetail.calls.length === 0 ? (
                        <p className="empty-copy">No calls for this org.</p>
                      ) : null}
                      {orgDetail.calls_truncated ? (
                        <p className="admin-provision-hint">
                          Showing the {orgDetail.calls.length} most recent of{' '}
                          {orgDetail.total_calls} calls.
                        </p>
                      ) : null}
                    </div>
                  </>
                ) : null}
              </div>

              <div className="admin-support">
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
              </div>
            </>
          )}
        </aside>
      </div>
    </>
  )
}
