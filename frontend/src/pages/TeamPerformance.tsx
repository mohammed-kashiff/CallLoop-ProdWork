import { useEffect, useMemo, useState } from 'react'
import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { KpiCard } from '../components/KpiCard'
import { apiFetch, readError } from '../lib/api'
import { roleTagLabel } from '../lib/roles'
import { useAuth } from '../context/AuthContext'

type Days = 7 | 30 | 90

type Highlight = { id?: string | null; name?: string | null } | null

type AgentRow = {
  user_id: string | null
  display_name: string
  role: string | null
  tickets: { avg_score: number | null; count: number }
  calls: { avg_score: number | null; count: number }
  top_strength: Highlight
  top_gap: Highlight
}

type Snapshot = {
  view_scope: 'team' | 'own'
  days: number
  org: {
    tickets: { avg_score: number | null; count: number }
    calls: { avg_score: number | null; count: number }
  }
  weekly: Array<{
    week: string
    ticket_avg: number | null
    call_avg: number | null
    ticket_count: number
    call_count: number
  }>
  agents: AgentRow[]
}

function fmtScore(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(Number(n))) return '—'
  return Number(n).toFixed(1)
}

function fmtWeek(raw: string): string {
  const d = new Date(`${raw}T00:00:00`)
  if (Number.isNaN(d.getTime())) return raw
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

function highlightName(h: Highlight): string {
  const name = (h?.name || '').trim()
  return name || '—'
}

function scoreTone(n: number | null): 'default' | 'good' | 'warn' | 'bad' {
  if (n == null) return 'default'
  if (n >= 80) return 'good'
  if (n >= 60) return 'warn'
  return 'bad'
}

export function TeamPerformance() {
  const { isOwnerOrManager } = useAuth()
  const [days, setDays] = useState<Days>(30)
  const [data, setData] = useState<Snapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(`/api/team-performance?days=${days}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load team performance.'))
        return r.json() as Promise<Snapshot>
      })
      .then((body) => {
        if (!cancelled) setData(body)
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Could not load team performance.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [days])

  const chartData = useMemo(
    () =>
      (data?.weekly || []).map((row) => ({
        ...row,
        label: fmtWeek(row.week),
      })),
    [data],
  )

  const teamView = data?.view_scope === 'team' || isOwnerOrManager
  const title = teamView ? 'Team Performance' : 'My Performance'
  const ticketAvg = data?.org.tickets.avg_score ?? null
  const callAvg = data?.org.calls.avg_score ?? null

  return (
    <div>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop</p>
          <h1>{title}</h1>
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

      {error ? <p className="upload-error" role="alert">{error}</p> : null}

      {loading && !data ? (
        <p className="panel-lede">Loading scores…</p>
      ) : null}

      {data ? (
        <>
          <div className="kpi-strip">
            <KpiCard
              label="Ticket avg"
              value={fmtScore(ticketAvg)}
              hint={`${data.org.tickets.count} scored`}
              tone={scoreTone(ticketAvg)}
            />
            <KpiCard
              label="Call avg"
              value={fmtScore(callAvg)}
              hint={`${data.org.calls.count} scored`}
              tone={scoreTone(callAvg)}
            />
            <KpiCard
              label="Ticket audits"
              value={String(data.org.tickets.count)}
            />
            <KpiCard
              label="Call audits"
              value={String(data.org.calls.count)}
            />
          </div>

          <section className="team-perf-panel" aria-label="Weekly average scores">
            <h2>Weekly scores</h2>
            {chartData.length === 0 ? (
              <p className="empty-copy">No scored tickets or calls in this window yet.</p>
            ) : (
              <div className="team-perf-chart">
                <ResponsiveContainer width="100%" height="100%">
                  <LineChart data={chartData} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--line)" />
                    <XAxis dataKey="label" tick={{ fill: 'var(--ink-soft)', fontSize: 12 }} />
                    <YAxis domain={[0, 100]} tick={{ fill: 'var(--ink-soft)', fontSize: 12 }} />
                    <Tooltip />
                    <Legend />
                    <Line
                      type="monotone"
                      dataKey="ticket_avg"
                      name="Tickets"
                      stroke="var(--teal, #0f766e)"
                      strokeWidth={2}
                      connectNulls
                      dot={{ r: 3 }}
                    />
                    <Line
                      type="monotone"
                      dataKey="call_avg"
                      name="Calls"
                      stroke="var(--blue, #0891b2)"
                      strokeWidth={2}
                      connectNulls
                      dot={{ r: 3 }}
                    />
                  </LineChart>
                </ResponsiveContainer>
              </div>
            )}
          </section>

          <section className="team-perf-panel" aria-label={teamView ? 'Agents' : 'Your scores'}>
            <h2>{teamView ? 'Agents' : 'Your scores'}</h2>
            <p className="panel-lede">
              Ticket Top Strength and Top Gap are the most common winning and
              missing dimensions across scored tickets. Calls show average
              score only — call-side strength/gap is not computed yet.
            </p>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>Agent</th>
                    {teamView ? <th>Role</th> : null}
                    <th>Ticket avg</th>
                    <th>Tickets</th>
                    <th>Call avg</th>
                    <th>Calls</th>
                    <th>Top strength</th>
                    <th>Top gap</th>
                  </tr>
                </thead>
                <tbody>
                  {data.agents.length === 0 ? (
                    <tr>
                      <td colSpan={teamView ? 8 : 7}>No teammates with scores in this window.</td>
                    </tr>
                  ) : (
                    data.agents.map((row) => (
                      <tr key={row.user_id ?? 'unassigned'}>
                        <td>{row.display_name}</td>
                        {teamView ? <td>{roleTagLabel(row.role)}</td> : null}
                        <td>{fmtScore(row.tickets.avg_score)}</td>
                        <td>{row.tickets.count}</td>
                        <td>{fmtScore(row.calls.avg_score)}</td>
                        <td>{row.calls.count}</td>
                        <td>{highlightName(row.top_strength)}</td>
                        <td>{highlightName(row.top_gap)}</td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </>
      ) : null}
    </div>
  )
}
