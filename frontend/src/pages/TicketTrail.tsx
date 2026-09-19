import { useEffect, useState } from 'react'
import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'
import { isAdminHost } from '../lib/adminHost'
import { capFirst } from '../lib/format'

type TrailEvent = {
  stage: string
  status: 'started' | 'succeeded' | 'failed'
  detail: Record<string, unknown> | null
  error: string | null
  created_at: string | null
}

type TrailPayload = {
  ticket_id: string
  org_id: string
  source: string | null
  subject: string | null
  events: TrailEvent[]
}

function stageLabel(stage: string): string {
  if (stage.startsWith('criterion:')) return `Criterion — ${stage.slice('criterion:'.length)}`
  return capFirst(stage.replace(/_/g, ' '))
}

function statusIcon(status: TrailEvent['status']): string {
  if (status === 'succeeded') return '✓'
  if (status === 'failed') return '✕'
  return '…'
}

function agentIdFromDetail(detail: Record<string, unknown> | null): string | null {
  const raw = detail?.agent_user_id
  return typeof raw === 'string' && raw ? raw : null
}

export function TicketTrail() {
  const { isPlatformAdmin } = useAuth()
  const { ticketId } = useParams()
  const [searchParams] = useSearchParams()
  const orgId = searchParams.get('org_id') || ''
  const [data, setData] = useState<TrailPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!orgId) {
      setLoading(false)
      setError('Missing org_id — open this page from Ticket logs.')
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(
      `/api/admin/tickets/${encodeURIComponent(ticketId || '')}/trail?org_id=${encodeURIComponent(orgId)}`,
    )
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load this ticket’s trail.'))
        return r.json() as Promise<TrailPayload>
      })
      .then((json) => {
        if (!cancelled) setData(json)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load this trail.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [ticketId, orgId])

  if (!isAdminHost()) return <Navigate to="/" replace />
  if (!isPlatformAdmin) return <Navigate to="/admin" replace />

  const title = data
    ? capFirst(data.subject || `Ticket ${data.ticket_id}`)
    : 'Ticket trail'

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/ticket-logs">Ticket logs</Link>
            {data ? ` / ${data.ticket_id}` : ''}
          </p>
          <h1>{title}</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading trail…</p> : null}

      {data && !loading ? (
        <div className="admin-card">
          <p className="admin-id">{data.org_id}</p>
          {data.source ? <p className="admin-provision-hint">{data.source}</p> : null}
          {data.events.length === 0 ? (
            <p className="empty-copy">No pipeline events recorded for this ticket yet.</p>
          ) : (
            <ol className="call-trail-list">
              {data.events.map((e, i) => {
                const agentId = agentIdFromDetail(e.detail)
                return (
                  <li key={i} className={`call-trail-item is-${e.status}`}>
                    <span className="call-trail-icon" aria-hidden="true">
                      {statusIcon(e.status)}
                    </span>
                    <div className="call-trail-body">
                      <div className="call-trail-head">
                        <span className="call-trail-stage">{stageLabel(e.stage)}</span>
                        <span className="call-trail-time">
                          {e.created_at ? new Date(e.created_at).toLocaleString() : '—'}
                        </span>
                      </div>
                      {agentId ? (
                        <p className="admin-provision-hint">Agent {agentId}</p>
                      ) : null}
                      {e.error ? <p className="call-trail-error">{e.error}</p> : null}
                      {e.detail ? (
                        <pre className="call-trail-detail">
                          {JSON.stringify(e.detail, null, 2)}
                        </pre>
                      ) : null}
                    </div>
                  </li>
                )
              })}
            </ol>
          )}
        </div>
      ) : null}
    </>
  )
}
