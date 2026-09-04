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
  call_id: number
  org_id: string
  filename: string | null
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

export function CallTrail() {
  const { isPlatformAdmin } = useAuth()
  const { callId } = useParams()
  const [searchParams] = useSearchParams()
  const orgId = searchParams.get('org_id') || ''
  const [data, setData] = useState<TrailPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!orgId) {
      setLoading(false)
      setError('Missing org_id — open this page from Call logs.')
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(
      `/api/admin/calls/${encodeURIComponent(callId || '')}/trail?org_id=${encodeURIComponent(orgId)}`,
    )
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load this call’s trail.'))
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
  }, [callId, orgId])

  if (!isAdminHost()) return <Navigate to="/" replace />
  if (!isPlatformAdmin) return <Navigate to="/admin" replace />

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/call-logs">Call logs</Link>
            {data ? ` / #${data.call_id}` : ''}
          </p>
          <h1>{data ? capFirst(data.filename || `Call #${data.call_id}`) : 'Call trail'}</h1>
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
          {data.events.length === 0 ? (
            <p className="empty-copy">No pipeline events recorded for this call yet.</p>
          ) : (
            <ol className="call-trail-list">
              {data.events.map((e, i) => (
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
                    {e.error ? <p className="call-trail-error">{e.error}</p> : null}
                    {e.detail ? (
                      <pre className="call-trail-detail">
                        {JSON.stringify(e.detail, null, 2)}
                      </pre>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </>
  )
}
