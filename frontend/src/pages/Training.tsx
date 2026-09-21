import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { TrainingCue } from '../components/LoopCues'
import { SketchWallpaper } from '../components/SketchWallpaper'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'

type Days = 7 | 30 | 90

type Focus = { id: string; name: string } | null

type Drill = {
  channel: 'ticket' | 'call'
  ticket_id: string | null
  call_id: number | null
  dimension_id: string
  dimension_name: string
  reasoning: string | null
  evidence_text: string | null
  coaching_note: string | null
  created_at: string | null
}

type Assignment = {
  id: string
  assignee_user_id: string
  channel: 'ticket' | 'call'
  ticket_id: string | null
  call_id: number | null
  dimension_id: string
  dimension_name: string
  prompt: string
  status: 'open' | 'done'
  reply: string | null
  created_at: string | null
  completed_at: string | null
}

type TrainingPayload = {
  view_scope: 'team' | 'own'
  days: number
  agent: { user_id: string; display_name: string; role: string }
  roster: Array<{ user_id: string; display_name: string }>
  focus: { ticket: Focus; call: Focus; channel: 'ticket' | 'call' | null; dim: string | null }
  drills: Drill[]
  assignments: Assignment[]
}

function asDays(raw: string | null): Days {
  if (raw === '7' || raw === '90') return Number(raw) as Days
  return 30
}

function sourceHref(channel: 'ticket' | 'call', ticketId: string | null, callId: number | null): string | null {
  if (channel === 'ticket' && ticketId) return `/ticket-audit/${ticketId}`
  if (channel === 'call' && callId) return `/audits/${callId}`
  return null
}

function drillPrompt(drill: Drill): string {
  return (drill.coaching_note || drill.reasoning || '').trim()
}

function drillKey(channel: string, ticketId: string | null, callId: number | null, dim: string): string {
  return `${channel}:${ticketId || callId || ''}:${dim}`
}

export function Training() {
  const { isOwnerOrManager, userId } = useAuth()
  const [params, setParams] = useSearchParams()
  const days = asDays(params.get('days'))
  const channel = params.get('channel') === 'call' ? 'call' : params.get('channel') === 'ticket' ? 'ticket' : ''
  const dim = (params.get('dim') || '').trim()
  const agent = (params.get('agent') || '').trim()
  const [data, setData] = useState<TrainingPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [replies, setReplies] = useState<Record<string, string>>({})

  const query = useMemo(() => {
    const q = new URLSearchParams()
    q.set('days', String(days))
    if (channel) q.set('channel', channel)
    if (dim) q.set('dim', dim)
    if (agent) q.set('agent', agent)
    return q.toString()
  }, [days, channel, dim, agent])

  const load = () => {
    setLoading(true)
    setError(null)
    return apiFetch(`/api/training?${query}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load training.'))
        return r.json() as Promise<TrainingPayload>
      })
      .then((body) => setData({ ...body, assignments: body.assignments || [] }))
      .catch((err: Error) => {
        setError(err.message || 'Could not load training.')
      })
      .finally(() => setLoading(false))
  }

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(`/api/training?${query}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load training.'))
        return r.json() as Promise<TrainingPayload>
      })
      .then((body) => {
        if (!cancelled) setData({ ...body, assignments: body.assignments || [] })
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Could not load training.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [query])

  const setDays = (n: Days) => {
    const next = new URLSearchParams(params)
    next.set('days', String(n))
    setParams(next, { replace: true })
  }

  const setAgent = (userIdValue: string) => {
    const next = new URLSearchParams(params)
    if (userIdValue) next.set('agent', userIdValue)
    else next.delete('agent')
    setParams(next, { replace: true })
  }

  const assignDrill = async (drill: Drill) => {
    const target = data?.agent.user_id
    if (!target) return
    const key = drillKey(drill.channel, drill.ticket_id, drill.call_id, drill.dimension_id)
    setBusy(key)
    setError(null)
    try {
      const r = await apiFetch('/api/training/assignments', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent: target,
          channel: drill.channel,
          dimension_id: drill.dimension_id,
          call_id: drill.call_id,
          ticket_id: drill.ticket_id,
        }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not assign this drill.'))
      await load()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not assign this drill.')
    } finally {
      setBusy(null)
    }
  }

  const completeAssignment = async (row: Assignment) => {
    setBusy(row.id)
    setError(null)
    try {
      const r = await apiFetch(`/api/training/assignments/${encodeURIComponent(row.id)}/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ reply: (replies[row.id] || '').trim() || null }),
      })
      if (!r.ok) throw new Error(await readError(r, 'Could not mark this drill done.'))
      await load()
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Could not mark this drill done.')
    } finally {
      setBusy(null)
    }
  }

  const teamView = data?.view_scope === 'team' || isOwnerOrManager
  const assignments = data?.assignments || []
  const openKeys = new Set(
    assignments
      .filter((a) => a.status === 'open')
      .map((a) => drillKey(a.channel, a.ticket_id, a.call_id, a.dimension_id)),
  )
  const drills = (data?.drills || []).filter(
    (d) => !openKeys.has(drillKey(d.channel, d.ticket_id, d.call_id, d.dimension_id)),
  )
  const openAssigned = assignments.filter((a) => a.status === 'open')
  const doneAssigned = assignments.filter((a) => a.status === 'done')

  return (
    <div>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Practice</p>
          <h1>Training</h1>
        </div>
        <div className="audit-filters" role="group" aria-label="Date range">
          {([7, 30, 90] as const).map((n) => (
            <button
              key={n}
              type="button"
              className={['ghost-btn', days === n ? 'is-current' : ''].filter(Boolean).join(' ')}
              aria-pressed={days === n}
              onClick={() => setDays(n)}
            >
              {n} days
            </button>
          ))}
        </div>
      </header>

      {teamView && (data?.roster.length || 0) > 0 ? (
        <p className="panel-lede">
          <label>
            Teammate{' '}
            <select
              value={data?.agent.user_id || agent}
              onChange={(e) => setAgent(e.target.value)}
            >
              {data?.roster.map((m) => (
                <option key={m.user_id} value={m.user_id}>
                  {m.display_name}
                </option>
              ))}
            </select>
          </label>
        </p>
      ) : null}

      {error ? <p className="upload-error" role="alert">{error}</p> : null}
      {loading && !data ? <p className="panel-lede">Loading drills…</p> : null}

      {data ? (
        <>
          <div className="train-focus-strip">
            <article className="kpi-card">
              <p className="kpi-label">Ticket gap</p>
              <p className="kpi-value">{data.focus.ticket?.name || '—'}</p>
              <p className="kpi-hint">{data.agent.display_name}</p>
            </article>
            <article className="kpi-card">
              <p className="kpi-label">Call gap</p>
              <p className="kpi-value">{data.focus.call?.name || '—'}</p>
              <p className="kpi-hint">Same window as Team Performance</p>
            </article>
          </div>

          {openAssigned.length > 0 ? (
            <section aria-label="Assigned drills">
              <p className="panel-lede">Assigned</p>
              {openAssigned.map((row) => {
                const href = sourceHref(row.channel, row.ticket_id, row.call_id)
                const mine = userId === row.assignee_user_id
                return (
                  <article className="train-drill" key={row.id}>
                    <h3>
                      {row.dimension_name}
                      {row.channel === 'ticket' ? ' · Ticket' : ' · Call'}
                    </h3>
                    <div className="train-steps">
                      <div>
                        <p className="train-step-label">Listen</p>
                        <p className="train-step-body">
                          {href ? <Link to={href}>Open the scored {row.channel}</Link> : 'Source is missing.'}
                        </p>
                      </div>
                      <div>
                        <p className="train-step-label">Drill</p>
                        <p className="train-step-body">{row.prompt || 'No coaching note on this finding.'}</p>
                      </div>
                      {mine ? (
                        <div>
                          <p className="train-step-label">Your recap</p>
                          <textarea
                            className="train-reply"
                            maxLength={400}
                            rows={2}
                            placeholder="Optional — one or two sentences"
                            value={replies[row.id] ?? ''}
                            onChange={(e) => setReplies((prev) => ({ ...prev, [row.id]: e.target.value }))}
                          />
                          <button
                            type="button"
                            className="ghost-btn"
                            disabled={busy === row.id}
                            onClick={() => completeAssignment(row)}
                          >
                            Done
                          </button>
                        </div>
                      ) : (
                        <p className="train-step-body">Waiting on the agent.</p>
                      )}
                    </div>
                  </article>
                )
              })}
            </section>
          ) : null}

          {drills.length === 0 && openAssigned.length === 0 ? (
            <div className="empty-card is-pulse">
              <SketchWallpaper variant="training" />
              <TrainingCue />
              <p className="empty-title">Nothing to practice yet</p>
              <p className="empty-copy">
                No failing dimensions in this window. Score more tickets or calls, then come back.
              </p>
            </div>
          ) : (
            drills.map((drill, idx) => {
              const href = sourceHref(drill.channel, drill.ticket_id, drill.call_id)
              const prompt = drillPrompt(drill)
              const key = `${drill.channel}-${drill.ticket_id || drill.call_id}-${idx}`
              return (
                <article className="train-drill" key={key}>
                  <h3>
                    {drill.dimension_name}
                    {drill.channel === 'ticket' ? ' · Ticket' : ' · Call'}
                  </h3>
                  <div className="train-steps">
                    <div>
                      <p className="train-step-label">Listen</p>
                      <p className="train-step-body">
                        {href ? <Link to={href}>Open the scored {drill.channel}</Link> : 'Source is missing.'}
                      </p>
                    </div>
                    <div>
                      <p className="train-step-label">Drill</p>
                      <p className="train-step-body">
                        {prompt || 'No coaching note on this finding.'}
                      </p>
                      {drill.evidence_text ? (
                        <p className="train-step-body">{drill.evidence_text}</p>
                      ) : null}
                    </div>
                    <div>
                      <p className="train-step-label">Recap</p>
                      <p className="train-step-body">
                        {href ? <Link to={href}>Review the audit</Link> : '—'}
                      </p>
                      {isOwnerOrManager ? (
                        <button
                          type="button"
                          className="ghost-btn"
                          disabled={busy === drillKey(drill.channel, drill.ticket_id, drill.call_id, drill.dimension_id)}
                          onClick={() => assignDrill(drill)}
                        >
                          Assign
                        </button>
                      ) : null}
                    </div>
                  </div>
                </article>
              )
            })
          )}

          {doneAssigned.length > 0 ? (
            <section aria-label="Completed drills">
              <p className="panel-lede">Done</p>
              {doneAssigned.map((row) => (
                <article className="train-drill" key={row.id}>
                  <h3>
                    {row.dimension_name}
                    {row.channel === 'ticket' ? ' · Ticket' : ' · Call'}
                    {' · Done'}
                  </h3>
                  {row.reply ? <p className="train-step-body">{row.reply}</p> : null}
                </article>
              ))}
            </section>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
