import { useEffect, useState, type FormEvent } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { apiFetch, fmtUsd, readError } from '../lib/api'
import { appHomePath, isAdminHost } from '../lib/adminHost'
import { roleTagLabel } from '../lib/roles'
import { useAuth } from '../context/AuthContext'

type UsagePayload = {
  usage: {
    by_provider?: Record<
      string,
      { hits?: number; actions?: number; polls?: number; units?: number }
    >
  }
  cost: { pyai_usd: number; claude_usd: number; total_usd: number }
}

type TeamMember = {
  user_id: string
  first_name: string | null
  last_name: string | null
  role: string
}

function TeamSection() {
  const [members, setMembers] = useState<TeamMember[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = () => {
    setError(null)
    apiFetch('/api/team')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load your team.'))
        return r.json() as Promise<{ members: TeamMember[] }>
      })
      .then((data) => setMembers(data.members))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : 'Could not load your team.'))
  }

  useEffect(() => {
    load()
  }, [])

  const setRole = async (userId: string, role: 'manager' | 'member') => {
    setBusyId(userId)
    setActionError(null)
    try {
      const r = await apiFetch(`/api/team/${encodeURIComponent(userId)}/role`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not change that role.'))
      load()
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : 'Could not change that role.')
    } finally {
      setBusyId(null)
    }
  }

  const memberName = (m: TeamMember) =>
    [m.first_name, m.last_name].filter(Boolean).join(' ') || m.user_id.slice(0, 8)

  return (
    <section className="profile-card">
      <h2>Your team</h2>
      <p className="admin-provision-hint">
        Managers see the same ticket-audit team view and rubric builder access you do — nothing
        else changes.
      </p>
      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {actionError ? (
        <p className="upload-error" role="alert">
          {actionError}
        </p>
      ) : null}
      {members ? (
        <ul className="profile-team-list">
          {members.map((m) => (
            <li key={m.user_id} className="profile-team-row">
              <span className="profile-team-name">{memberName(m)}</span>
              <span className="topbar-chip soft">{roleTagLabel(m.role)}</span>
              {m.role === 'member' ? (
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={busyId === m.user_id}
                  onClick={() => void setRole(m.user_id, 'manager')}
                >
                  {busyId === m.user_id ? 'Working…' : 'Promote to Manager'}
                </button>
              ) : m.role === 'manager' ? (
                <button
                  type="button"
                  className="ghost-btn"
                  disabled={busyId === m.user_id}
                  onClick={() => void setRole(m.user_id, 'member')}
                >
                  {busyId === m.user_id ? 'Working…' : 'Remove Manager'}
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : !error ? (
        <p className="panel-lede">Loading your team…</p>
      ) : null}
    </section>
  )
}

export function Profile() {
  const {
    email,
    orgName,
    role,
    firstName,
    lastName,
    isPlatformAdmin,
    refreshMe,
  } = useAuth()
  const [first, setFirst] = useState(firstName || '')
  const [last, setLast] = useState(lastName || '')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [usage, setUsage] = useState<UsagePayload | null>(null)
  const [usageError, setUsageError] = useState<string | null>(null)

  useEffect(() => {
    setFirst(firstName || '')
    setLast(lastName || '')
  }, [firstName, lastName])

  useEffect(() => {
    let cancelled = false
    apiFetch('/api/me/usage')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load usage.'))
        return r.json() as Promise<UsagePayload>
      })
      .then((data) => {
        if (!cancelled) setUsage(data)
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setUsageError(e instanceof Error ? e.message : 'Could not load usage.')
        }
      })
    return () => {
      cancelled = true
    }
  }, [])

  const onSave = async (e: FormEvent) => {
    e.preventDefault()
    setError(null)
    setSaved(false)
    const firstS = first.trim()
    const lastS = last.trim()
    if (!firstS || !lastS) {
      setError('First and last name are required.')
      return
    }
    setBusy(true)
    try {
      const r = await apiFetch('/api/me', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ first_name: firstS, last_name: lastS }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not save your name.'))
      await refreshMe()
      setSaved(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not save your name.')
    } finally {
      setBusy(false)
    }
  }

  const pyai = usage?.usage?.by_provider?.pyai
  const claude = usage?.usage?.by_provider?.anthropic
  const roleLabel = role ? roleTagLabel(role) : null

  if (isAdminHost()) return <Navigate to="/admin" replace />

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb"><Link to={appHomePath()}>{appHomePath() === '/admin' ? 'Admin' : 'Home'}</Link> / Account</p>
          <h1>Profile</h1>
        </div>
      </header>

      <section className="profile-card">
        <div className="profile-badges">
          {roleLabel ? <span className="topbar-chip">{roleLabel}</span> : null}
          {isPlatformAdmin ? (
            <span className="topbar-chip soft">Platform Admin</span>
          ) : null}
        </div>

        <form className="auth-form" onSubmit={(e) => void onSave(e)}>
          <label>
            First name
            <input
              type="text"
              autoComplete="given-name"
              value={first}
              onChange={(ev) => {
                setFirst(ev.target.value)
                setSaved(false)
              }}
              required
              maxLength={80}
            />
          </label>
          <label>
            Last name
            <input
              type="text"
              autoComplete="family-name"
              value={last}
              onChange={(ev) => {
                setLast(ev.target.value)
                setSaved(false)
              }}
              required
              maxLength={80}
            />
          </label>
          <label>
            Email
            <input type="email" value={email || ''} readOnly />
          </label>
          <label>
            Organization
            <input type="text" value={orgName || '—'} readOnly />
          </label>
          {error ? (
            <p className="upload-error" role="alert">
              {error}
            </p>
          ) : null}
          {saved ? (
            <p className="auth-info" role="status">
              Name saved.
            </p>
          ) : null}
          <button className="start-btn" type="submit" disabled={busy}>
            {busy ? 'Saving…' : 'Save name'}
          </button>
        </form>
      </section>

      {role === 'owner' ? <TeamSection /> : null}

      <section className="profile-card">
        <h2>Workspace usage</h2>
        <p className="admin-provision-hint">
          Same totals an admin sees for this organization.
        </p>
        {usageError ? (
          <p className="upload-error" role="alert">
            {usageError}
          </p>
        ) : null}
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
        ) : !usageError ? (
          <p className="panel-lede">Loading usage…</p>
        ) : null}
      </section>
    </>
  )
}
