import { useState, type FormEvent } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'
import { isAdminHost } from '../lib/adminHost'
import { formatBytes, formatTime } from '../lib/format'

type CallLogRow = {
  call_id: number
  filename: string | null
  created_at: string | null
  audio_seconds: number | null
  mode: 'pyai' | 'selfhosted'
  uploaded_by: string | null
  data_size_bytes: number
  deleted: boolean
  deleted_by_short_id: number | null
}

type Matched = {
  org_id: string
  user_id: string | null
  role: string | null
  name: string | null
  email: string | null
  scope: 'org' | 'member'
}

type CallLogsPayload = {
  matched: Matched
  calls: CallLogRow[]
  total_calls: number
  calls_truncated: boolean
}

function scopeLabel(matched: Matched): string {
  const who = matched.name || matched.email
  if (matched.scope === 'member') {
    return who ? `${who} — their uploads only` : 'This team member — their uploads only'
  }
  return who ? `${who} — account owner, whole org` : 'Whole org'
}

export function CallLogs() {
  const { isPlatformAdmin } = useAuth()
  const [query, setQuery] = useState('')
  const [data, setData] = useState<CallLogsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState<string | null>(null)

  const search = async (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (!q) return
    setError(null)
    setLoading(true)
    try {
      const r = await apiFetch(`/api/admin/call-logs?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not search call logs.'))
      setData((await r.json()) as CallLogsPayload)
    } catch (err: unknown) {
      setData(null)
      setError(err instanceof Error ? err.message : 'Could not search call logs.')
    } finally {
      setLoading(false)
    }
  }

  const exportCsv = async () => {
    const q = query.trim()
    if (!q) return
    setExportError(null)
    setExporting(true)
    try {
      const r = await apiFetch(`/api/admin/call-logs/export?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not export call logs.'))
      const blob = await r.blob()
      let name = 'callproof-call-logs.csv'
      const cd = r.headers.get('Content-Disposition') || ''
      const m = /filename="([^"]+)"/.exec(cd)
      if (m) name = m[1]
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = name
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    } catch (err: unknown) {
      setExportError(err instanceof Error ? err.message : 'Could not export call logs.')
    } finally {
      setExporting(false)
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Platform</p>
              <h1>Call logs</h1>
            </div>
          </header>
          <p className="admin-provision-hint">This console is limited to platform admins.</p>
        </>
      )
    }
    return <Navigate to="/" replace />
  }

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Platform</p>
          <h1>Call logs</h1>
        </div>
      </header>

      <section className="admin-card">
        <h2>Search</h2>
        <p className="admin-provision-hint">
          Email, org id, or short id. A team member's id shows only what they
          uploaded; the account owner's id (or a bare org id) shows the
          whole org.
        </p>
        <form className="admin-provision-form" onSubmit={(e) => void search(e)}>
          <label>
            Email, org id, or short id
            <input
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="ada@example.com, UUID, or 100001"
              required
            />
          </label>
          <button type="submit" className="start-btn" disabled={loading}>
            {loading ? 'Searching…' : 'Search'}
          </button>
          <button
            type="button"
            className="ghost-btn"
            disabled={!data || exporting}
            onClick={() => void exportCsv()}
          >
            {exporting ? 'Exporting…' : 'Export CSV'}
          </button>
        </form>
        {error ? (
          <p className="upload-error" role="alert">
            {error}
          </p>
        ) : null}
        {exportError ? (
          <p className="upload-error" role="alert">
            {exportError}
          </p>
        ) : null}
      </section>

      {data ? (
        <div className="admin-card admin-calls-full">
          <h3>{scopeLabel(data.matched)}</h3>
          <p className="admin-id">{data.matched.org_id}</p>
          <div className="admin-calls">
            <dl className="admin-stats">
              <div>
                <dt>Total calls</dt>
                <dd>{data.total_calls}</dd>
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
                    <th>Size</th>
                    <th>Uploaded by</th>
                    <th>Status</th>
                    <th>Trail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.calls.map((c) => (
                    <tr key={c.call_id}>
                      <td>
                        {c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}
                      </td>
                      <td>{c.filename || '—'}</td>
                      <td>{c.audio_seconds != null ? formatTime(c.audio_seconds) : '—'}</td>
                      <td>{c.mode === 'selfhosted' ? 'Self-hosted' : 'PyAI'}</td>
                      <td>{formatBytes(c.data_size_bytes)}</td>
                      <td>{c.uploaded_by || '—'}</td>
                      <td>
                        {c.deleted ? (
                          <span className="admin-deleted-badge">
                            Deleted
                            {c.deleted_by_short_id != null ? ` by #${c.deleted_by_short_id}` : ''}
                          </span>
                        ) : (
                          '—'
                        )}
                      </td>
                      <td>
                        <Link
                          to={`/call-logs/${c.call_id}/trail?org_id=${encodeURIComponent(data.matched.org_id)}`}
                        >
                          View
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.calls.length === 0 ? <p className="empty-copy">No calls found.</p> : null}
              {data.calls_truncated ? (
                <p className="admin-provision-hint">
                  Showing the {data.calls.length} most recent of {data.total_calls} calls.
                </p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
