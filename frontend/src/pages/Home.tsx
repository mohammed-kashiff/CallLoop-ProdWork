import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { KpiCard } from '../components/KpiCard'
import { ProblemSection } from '../components/ProblemSection'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'

type ChannelScore = {
  avg_score: number | null
  count: number
  target: number | null
}

type HomePayload = {
  user_id: string
  days: number
  scores: { tickets: ChannelScore; calls: ChannelScore }
  flags: Array<{ call_id: number; filename: string; score: number | null }>
  drills: Array<{
    kind: 'assignment' | 'suggested'
    id: string | null
    channel: 'call' | 'ticket'
    call_id: number | null
    ticket_id: string | null
    dimension_id: string
    dimension_name: string
    prompt: string
    status: string
  }>
  tickets: Array<{
    ticket_id: string
    status: string | null
    created_at: string | null
    subject: string | null
  }>
}

function fmtScore(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(Number(n))) return '—'
  return Number(n).toFixed(1)
}

function scoreTone(n: number | null, target?: number | null): 'default' | 'good' | 'warn' | 'bad' {
  if (n == null) return 'default'
  if (target != null && Number.isFinite(target)) {
    if (n >= target) return 'good'
    if (n >= Math.max(0, target - 20)) return 'warn'
    return 'bad'
  }
  return 'default'
}

function drillHref(row: HomePayload['drills'][number]): string {
  if (row.channel === 'ticket' && row.ticket_id) return `/ticket-audit/${row.ticket_id}`
  if (row.channel === 'call' && row.call_id) return `/audits/${row.call_id}`
  return '/training'
}

export function Home() {
  const { isOwnerOrManager, userId } = useAuth()
  const [data, setData] = useState<HomePayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch('/api/home')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load home.'))
        return r.json() as Promise<HomePayload>
      })
      .then((body) => {
        if (!cancelled) setData(body)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load home.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [userId])

  const emptyWeek =
    data &&
    data.scores.tickets.count === 0 &&
    data.scores.calls.count === 0 &&
    data.flags.length === 0 &&
    data.drills.length === 0 &&
    data.tickets.length === 0

  return (
    <div className="home-week">
      <ProblemSection />

      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Home</p>
          <h1>Your week</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading your week…</p> : null}

      {data && !loading ? (
        emptyWeek ? (
          <section className="profile-card">
            <h2>Nothing scored yet</h2>
            <p className="panel-lede">
              {isOwnerOrManager
                ? 'When calls and tickets in this workspace are scored, your own numbers land here.'
                : 'When you are scored on a call or ticket, your numbers land here.'}
            </p>
            {isOwnerOrManager ? (
              <p className="home-ingest-cue">
                Owners and managers ingest from <Link to="/agents-pulse">Agent Pulse</Link>.
              </p>
            ) : null}
          </section>
        ) : (
          <>
            <section className="home-kpis" aria-label="Your scores">
              <KpiCard
                label="Ticket score"
                value={fmtScore(data.scores.tickets.avg_score)}
                hint={
                  data.scores.tickets.target != null
                    ? `${data.scores.tickets.count} scored · target ${fmtScore(data.scores.tickets.target)}`
                    : `${data.scores.tickets.count} scored`
                }
                tone={scoreTone(data.scores.tickets.avg_score, data.scores.tickets.target)}
              />
              <KpiCard
                label="Call score"
                value={fmtScore(data.scores.calls.avg_score)}
                hint={
                  data.scores.calls.target != null
                    ? `${data.scores.calls.count} scored · target ${fmtScore(data.scores.calls.target)}`
                    : `${data.scores.calls.count} scored`
                }
                tone={scoreTone(data.scores.calls.avg_score, data.scores.calls.target)}
              />
              <p className="home-kpi-link">
                <Link to="/team-performance">Team performance</Link>
              </p>
            </section>

            <section className="profile-card">
              <h2>Open flags</h2>
              {data.flags.length === 0 ? (
                <p className="panel-lede">No open flags on your calls.</p>
              ) : (
                <ul className="home-list">
                  {data.flags.map((row) => (
                    <li key={row.call_id}>
                      <Link to="/agents-pulse/flagged">{row.filename}</Link>
                      {row.score != null ? <span> · {fmtScore(row.score)}</span> : null}
                    </li>
                  ))}
                </ul>
              )}
              <p className="home-kpi-link">
                <Link to="/agents-pulse/flagged">Flagged for review</Link>
              </p>
            </section>

            <section className="profile-card">
              <h2>{data.drills.some((d) => d.kind === 'assignment') ? 'Assigned drills' : 'Suggested drills'}</h2>
              {data.drills.length === 0 ? (
                <p className="panel-lede">No open drills right now.</p>
              ) : (
                <ul className="home-list">
                  {data.drills.map((row) => (
                    <li key={`${row.kind}:${row.id || row.dimension_id}:${row.call_id || row.ticket_id}`}>
                      <Link to={drillHref(row)}>{row.dimension_name}</Link>
                      {row.prompt ? <p className="panel-lede">{row.prompt}</p> : null}
                    </li>
                  ))}
                </ul>
              )}
              <p className="home-kpi-link">
                <Link to="/training">Training</Link>
              </p>
            </section>

            <section className="profile-card">
              <h2>Tickets you were on</h2>
              {data.tickets.length === 0 ? (
                <p className="panel-lede">No recent tickets.</p>
              ) : (
                <ul className="home-list">
                  {data.tickets.map((row) => (
                    <li key={row.ticket_id}>
                      <Link to={`/ticket-audit/${row.ticket_id}`}>
                        {row.subject || row.ticket_id.slice(0, 8)}
                      </Link>
                      {row.status ? <span> · {row.status}</span> : null}
                    </li>
                  ))}
                </ul>
              )}
              <p className="home-kpi-link">
                <Link to="/ticket-audit-mine">My contributions</Link>
              </p>
            </section>

            {isOwnerOrManager ? (
              <p className="home-ingest-cue">
                Ingest a new call from <Link to="/agents-pulse">Agent Pulse</Link>.
              </p>
            ) : null}
          </>
        )
      ) : null}
    </div>
  )
}
