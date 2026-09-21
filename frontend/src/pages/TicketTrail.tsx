import { useEffect, useMemo, useState } from 'react'
import { Link, Navigate, useParams, useSearchParams } from 'react-router-dom'
import { CommandCenterPage } from '../components/cc/CommandCenterPage'
import { CcTimeline, type CcTrailEvent } from '../components/cc/CcTimeline'
import { CcTrailRail } from '../components/cc/CcTrailRail'
import { ccErr, ccHint } from '../components/cc/classes'
import { defaultSelectedIndex, orderTrailEvents } from '../components/cc/trailStats'
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

function agentIdFromDetail(detail: Record<string, unknown> | null): string | null {
  const raw = detail?.agent_user_id
  return typeof raw === 'string' && raw ? raw : null
}

type ApiCall = { method: string; endpoint: string }

function apisFromDetail(detail: Record<string, unknown> | null): ApiCall[] {
  const raw = detail?.apis
  if (!Array.isArray(raw)) return []
  const out: ApiCall[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const rec = item as Record<string, unknown>
    const method = typeof rec.method === 'string' ? rec.method.trim().toUpperCase() : ''
    const endpoint = typeof rec.endpoint === 'string' ? rec.endpoint.trim() : ''
    if (method && endpoint) out.push({ method, endpoint })
  }
  return out
}

function detailWithoutApis(detail: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!detail) return null
  const rest = { ...detail }
  delete rest.apis
  return Object.keys(rest).length ? rest : null
}

export function TicketTrail() {
  const { isPlatformAdmin } = useAuth()
  const { ticketId } = useParams()
  const [searchParams] = useSearchParams()
  const orgId = searchParams.get('org_id') || ''
  const [data, setData] = useState<TrailPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selectedIndex, setSelectedIndex] = useState(0)

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

  const events: CcTrailEvent[] = useMemo(
    () =>
      (data?.events || []).map((e) => ({
        stage: stageLabel(e.stage),
        status: e.status,
        created_at: e.created_at,
        error: e.error,
        apis: apisFromDetail(e.detail),
        extra: detailWithoutApis(e.detail),
        agentId: agentIdFromDetail(e.detail),
      })),
    [data],
  )
  const ordered = useMemo(() => orderTrailEvents(events), [events])

  useEffect(() => {
    setSelectedIndex(defaultSelectedIndex(ordered))
  }, [ordered])

  if (!isAdminHost()) return <Navigate to="/" replace />
  if (!isPlatformAdmin) return <Navigate to="/admin" replace />

  const title = data ? capFirst(data.subject || `Ticket ${data.ticket_id}`) : 'Ticket trail'

  return (
    <CommandCenterPage
      title={title}
      crumb={
        <>
          <Link className="hover:underline" to="/ticket-logs">
            Ticket logs
          </Link>
          {data ? ` / ${data.ticket_id}` : ''}
        </>
      }
    >
      {error ? (
        <p className={ccErr} role="alert">
          {error}
        </p>
      ) : null}
      {loading ? <p className={ccHint}>Loading trail…</p> : null}
      {data && !loading ? (
        <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem]">
          <aside className="lg:col-start-2 lg:row-start-1">
            <div className="lg:sticky lg:top-6">
              <CcTrailRail
                events={ordered}
                selectedIndex={selectedIndex}
                orgId={data.org_id}
                logsTo={`/ticket-logs?query=${encodeURIComponent(data.org_id)}`}
                identity={capFirst(data.subject || `Ticket ${data.ticket_id}`)}
                source={data.source}
              />
            </div>
          </aside>
          <div className="rounded-xl border border-cc-line bg-cc-card p-6 shadow-sm lg:col-start-1 lg:row-start-1">
            <CcTimeline events={ordered} selectedIndex={selectedIndex} onSelect={setSelectedIndex} />
          </div>
        </div>
      ) : null}
    </CommandCenterPage>
  )
}
