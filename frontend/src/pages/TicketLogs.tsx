import { useState, type FormEvent } from 'react'
import { Link, Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'
import { isAdminHost } from '../lib/adminHost'

type TicketLogRow = {
  ticket_id: string
  source: string | null
  status: string | null
  subject: string | null
  external_id: string | null
  created_at: string | null
}

type Matched = {
  org_id: string
  user_id: string | null
  role: string | null
  name: string | null
  email: string | null
  scope: 'org' | 'member'
}

type TicketLogsPayload = {
  matched: Matched
  tickets: TicketLogRow[]
  total_tickets: number
  tickets_truncated: boolean
}

function scopeLabel(matched: Matched): string {
  const who = matched.name || matched.email
  if (matched.scope === 'member') {
    return who ? `${who} — tickets they scored as an agent` : 'This team member — their ticket work only'
  }
  return who ? `${who} — account owner, whole org` : 'Whole org'
}

function sourceLabel(source: string | null): string {
  if (source === 'intercom_api') return 'Intercom'
  if (source === 'pdf_upload') return 'PDF'
  return source || '—'
}

export function TicketLogs() {
  const { isPlatformAdmin } = useAuth()
  const [query, setQuery] = useState('')
  const [data, setData] = useState<TicketLogsPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const search = async (e: FormEvent) => {
    e.preventDefault()
    const q = query.trim()
    if (!q) return
    setError(null)
    setLoading(true)
    try {
      const r = await apiFetch(`/api/admin/ticket-logs?query=${encodeURIComponent(q)}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not search ticket logs.'))
      setData((await r.json()) as TicketLogsPayload)
    } catch (err: unknown) {
      setData(null)
      setError(err instanceof Error ? err.message : 'Could not search ticket logs.')
    } finally {
      setLoading(false)
    }
  }

  if (!isPlatformAdmin) {
    if (isAdminHost()) {
      return (
        <>
          <header className="page-bar">
            <div>
              <p className="crumb">Platform</p>
              <h1>Ticket logs</h1>
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
          <h1>Ticket logs</h1>
        </div>
      </header>

      <section className="admin-card">
        <h2>Search</h2>
        <p className="admin-provision-hint">
          Email, org id, or short id. A team member&rsquo;s id shows tickets they
          appear on as an agent; the account owner&rsquo;s id (or a bare org id)
          shows the whole org.
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
        </form>
        {error ? (
          <p className="upload-error" role="alert">
            {error}
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
                <dt>Total tickets</dt>
                <dd>{data.total_tickets}</dd>
              </div>
            </dl>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Date</th>
                    <th>Subject</th>
                    <th>Source</th>
                    <th>Status</th>
                    <th>External id</th>
                    <th>Trail</th>
                  </tr>
                </thead>
                <tbody>
                  {data.tickets.map((t) => (
                    <tr key={t.ticket_id}>
                      <td>
                        {t.created_at ? new Date(t.created_at).toLocaleDateString() : '—'}
                      </td>
                      <td>{t.subject || t.ticket_id}</td>
                      <td>{sourceLabel(t.source)}</td>
                      <td>{t.status || '—'}</td>
                      <td>{t.external_id || '—'}</td>
                      <td>
                        <Link
                          to={`/ticket-logs/${t.ticket_id}/trail?org_id=${encodeURIComponent(data.matched.org_id)}`}
                        >
                          View
                        </Link>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.tickets.length === 0 ? <p className="empty-copy">No tickets found.</p> : null}
              {data.tickets_truncated ? (
                <p className="admin-provision-hint">
                  Showing the {data.tickets.length} most recent of {data.total_tickets} tickets.
                </p>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}
    </>
  )
}
