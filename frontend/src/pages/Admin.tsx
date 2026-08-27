import { useCallback, useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { apiFetch, fmtUsd, readError } from '../lib/api'
import { TRIAL_FLAGS, type FeatureMap } from '../lib/features'
import { useAuth } from '../context/AuthContext'

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
  const [busy, setBusy] = useState(false)

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

  const loadOrg = async (row: DirectoryRow) => {
    setSelected(row)
    setError(null)
    setBusy(true)
    try {
      const r = await apiFetch(
        `/api/admin/usage?org_id=${encodeURIComponent(row.org_id)}`,
      )
      if (!r.ok) throw new Error(await readError(r, 'Could not load usage.'))
      setUsage((await r.json()) as UsagePayload)
    } catch (e: unknown) {
      setUsage(null)
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

  if (!isPlatformAdmin) {
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
                  const on = usage?.features?.[flag.key] !== false
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
                    </li>
                  )
                })}
              </ul>
            </>
          )}
        </aside>
      </div>
    </>
  )
}
