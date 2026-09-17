import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
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
import { useAuth } from '../context/AuthContext'
import { KpiCard } from '../components/KpiCard'
import { apiFetch, readError } from '../lib/api'
import { roleTagLabel } from '../lib/roles'

type Days = 7 | 30 | 90
type SortKey = 'name' | 'ticket_avg' | 'call_avg'
type SortDir = 'asc' | 'desc'

type Highlight = { id?: string | null; name?: string | null } | null

type AgentRow = {
  user_id: string | null
  display_name: string
  role: string | null
  tickets: { avg_score: number | null; count: number }
  calls: { avg_score: number | null; count: number }
  top_strength: Highlight
  top_gap: Highlight
  call_top_strength: Highlight
  call_top_gap: Highlight
}

type HeatmapCell = {
  id: string
  name: string
  pass: number
  n: number
  rate: number | null
}

type HeatmapGrid = {
  dimensions: Array<{ id: string; name: string }>
  rows: Array<{ user_id: string; cells: HeatmapCell[] }>
}

type Snapshot = {
  view_scope: 'team' | 'own'
  days: number
  org: {
    tickets: { avg_score: number | null; count: number; total: number }
    calls: { avg_score: number | null; count: number; total: number }
  }
  weekly: Array<{
    week: string
    ticket_avg: number | null
    call_avg: number | null
    ticket_count: number
    call_count: number
  }>
  agents: AgentRow[]
  heatmap?: {
    tickets: HeatmapGrid
    calls: HeatmapGrid
  }
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

function coverageHint(count: number, total: number): string {
  if (total > 0) return `${count} scored / ${total}`
  return `${count} scored`
}

function heatmapTone(rate: number | null): 'default' | 'good' | 'warn' | 'bad' {
  if (rate == null) return 'default'
  return scoreTone(rate * 100)
}

function fmtPassRate(cell: HeatmapCell): string {
  if (cell.n <= 0) return '—'
  return `${cell.pass}/${cell.n}`
}

function cmpNum(a: number | null, b: number | null, dir: SortDir): number {
  const av = a == null ? -1 : a
  const bv = b == null ? -1 : b
  return dir === 'asc' ? av - bv : bv - av
}

export function TeamPerformance() {
  const { isOwnerOrManager, userId, role } = useAuth()
  const [days, setDays] = useState<Days>(30)
  const [data, setData] = useState<Snapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('name')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [heatChannel, setHeatChannel] = useState<'tickets' | 'calls'>('tickets')

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

  const sortedAgents = useMemo(() => {
    const rows = [...(data?.agents || [])]
    rows.sort((a, b) => {
      if (sortKey === 'ticket_avg') return cmpNum(a.tickets.avg_score, b.tickets.avg_score, sortDir)
      if (sortKey === 'call_avg') return cmpNum(a.calls.avg_score, b.calls.avg_score, sortDir)
      const cmp = a.display_name.localeCompare(b.display_name)
      return sortDir === 'asc' ? cmp : -cmp
    })
    return rows
  }, [data, sortKey, sortDir])

  const onSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
      return
    }
    setSortKey(key)
    setSortDir(key === 'name' ? 'asc' : 'desc')
  }

  const teamView = data?.view_scope === 'team' || isOwnerOrManager
  const title = teamView ? 'Team Performance' : 'My Performance'
  const ticketAvg = data?.org.tickets.avg_score ?? null
  const callAvg = data?.org.calls.avg_score ?? null
  const unassigned = data?.agents.find((a) => a.user_id == null)
  const namesById = useMemo(() => {
    const map = new Map<string, string>()
    for (const row of data?.agents || []) {
      if (row.user_id) map.set(row.user_id, row.display_name)
    }
    return map
  }, [data])
  const heat = heatChannel === 'tickets' ? data?.heatmap?.tickets : data?.heatmap?.calls
  const colCount = teamView ? 10 : 9

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
              hint={coverageHint(data.org.tickets.count, data.org.tickets.total)}
              tone={scoreTone(ticketAvg)}
            />
            <KpiCard
              label="Call avg"
              value={fmtScore(callAvg)}
              hint={coverageHint(data.org.calls.count, data.org.calls.total)}
              tone={scoreTone(callAvg)}
            />
            <KpiCard
              label="Ticket coverage"
              value={`${data.org.tickets.count}/${data.org.tickets.total || 0}`}
              hint="Scored / tickets in window"
            />
            <KpiCard
              label="Call coverage"
              value={`${data.org.calls.count}/${data.org.calls.total || 0}`}
              hint="Scored / calls in window"
            />
          </div>

          {teamView && unassigned && unassigned.calls.count > 0 ? (
            <p className="scaffold-banner">
              {unassigned.calls.count} scored call{unassigned.calls.count === 1 ? '' : 's'}{' '}
              {role === 'owner' ? (
                <>
                  have no teammate yet.{' '}
                  <Link to="/integrations#justcall-agent-mapping">Map JustCall agent emails</Link>
                  {' '}so they show up on a real row.
                </>
              ) : (
                <>have no teammate yet — ask the account owner to map JustCall agent emails.</>
              )}
            </p>
          ) : null}

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

          <section className="team-perf-panel" aria-label="Dimension pass rates">
            <div className="team-perf-heat-head">
              <h2>Dimension pass rates</h2>
              <div className="audit-filters" role="group" aria-label="Heatmap channel">
                <button
                  type="button"
                  className={['ghost-btn', heatChannel === 'tickets' ? 'is-current' : ''].filter(Boolean).join(' ')}
                  aria-pressed={heatChannel === 'tickets'}
                  onClick={() => setHeatChannel('tickets')}
                >
                  Tickets
                </button>
                <button
                  type="button"
                  className={['ghost-btn', heatChannel === 'calls' ? 'is-current' : ''].filter(Boolean).join(' ')}
                  aria-pressed={heatChannel === 'calls'}
                  onClick={() => setHeatChannel('calls')}
                >
                  Calls
                </button>
              </div>
            </div>
            <p className="panel-lede">
              Pass count over scored findings in this window. Partial and fail sit in the
              denominator. Click a cell with scores to open Training for that gap.
            </p>
            {!heat || heat.dimensions.length === 0 ? (
              <p className="empty-copy">No scored dimensions in this window yet.</p>
            ) : (
              <div className="admin-table-wrap">
                <table className="admin-table team-perf-heat">
                  <thead>
                    <tr>
                      <th>Agent</th>
                      {heat.dimensions.map((dim) => (
                        <th key={dim.id}>{dim.name}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {heat.rows.length === 0 ? (
                      <tr>
                        <td colSpan={heat.dimensions.length + 1}>No teammates in this window.</td>
                      </tr>
                    ) : (
                      heat.rows.map((row) => (
                        <tr key={row.user_id}>
                          <td>{namesById.get(row.user_id) || 'Former teammate'}</td>
                          {row.cells.map((cell) => {
                            const tone = heatmapTone(cell.rate)
                            const channel = heatChannel === 'tickets' ? 'ticket' : 'call'
                            const canPractice = cell.n > 0 && Boolean(row.user_id)
                            const href = `/training?channel=${channel}&dim=${encodeURIComponent(cell.id)}&agent=${encodeURIComponent(row.user_id)}&days=${days}`
                            return (
                              <td key={cell.id} className={`team-perf-heat-cell tone-${tone}`}>
                                {canPractice ? (
                                  <Link to={href} title={`${cell.name}: ${fmtPassRate(cell)}`}>
                                    {fmtPassRate(cell)}
                                  </Link>
                                ) : (
                                  fmtPassRate(cell)
                                )}
                              </td>
                            )
                          })}
                        </tr>
                      ))
                    )}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <section className="team-perf-panel" aria-label={teamView ? 'Agents' : 'Your scores'}>
            <h2>{teamView ? 'Agents' : 'Your scores'}</h2>
            <p className="panel-lede">
              Top Strength and Top Gap are the most common winning and missing dimensions
              across scored tickets and calls in this window.
            </p>
            <div className="admin-table-wrap">
              <table className="admin-table">
                <thead>
                  <tr>
                    <th>
                      <button type="button" className="team-perf-sort" onClick={() => onSort('name')}>
                        Agent{sortKey === 'name' ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                      </button>
                    </th>
                    {teamView ? <th>Role</th> : null}
                    <th>
                      <button type="button" className="team-perf-sort" onClick={() => onSort('ticket_avg')}>
                        Ticket avg{sortKey === 'ticket_avg' ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                      </button>
                    </th>
                    <th>Tickets</th>
                    <th>
                      <button type="button" className="team-perf-sort" onClick={() => onSort('call_avg')}>
                        Call avg{sortKey === 'call_avg' ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ''}
                      </button>
                    </th>
                    <th>Calls</th>
                    <th>Ticket strength</th>
                    <th>Ticket gap</th>
                    <th>Call strength</th>
                    <th>Call gap</th>
                  </tr>
                </thead>
                <tbody>
                  {sortedAgents.length === 0 ? (
                    <tr>
                      <td colSpan={colCount}>No teammates with scores in this window.</td>
                    </tr>
                  ) : (
                    sortedAgents.map((row) => {
                      const mine = Boolean(row.user_id && userId && row.user_id === userId)
                      const ticketHref = mine ? '/ticket-audit-mine' : '/ticket-audit'
                      return (
                        <tr key={row.user_id ?? 'unassigned'}>
                          <td>
                            {row.user_id ? (
                              <Link to={ticketHref}>{row.display_name}</Link>
                            ) : role === 'owner' ? (
                              <Link to="/integrations#justcall-agent-mapping">{row.display_name}</Link>
                            ) : (
                              row.display_name
                            )}
                          </td>
                          {teamView ? <td>{roleTagLabel(row.role)}</td> : null}
                          <td>{fmtScore(row.tickets.avg_score)}</td>
                          <td>
                            {row.user_id ? (
                              <Link to={ticketHref}>{row.tickets.count}</Link>
                            ) : (
                              row.tickets.count
                            )}
                          </td>
                          <td>{fmtScore(row.calls.avg_score)}</td>
                          <td>
                            {row.user_id ? <Link to="/audits">{row.calls.count}</Link> : row.calls.count}
                          </td>
                          <td>{highlightName(row.top_strength)}</td>
                          <td>{highlightName(row.top_gap)}</td>
                          <td>{highlightName(row.call_top_strength)}</td>
                          <td>{highlightName(row.call_top_gap)}</td>
                        </tr>
                      )
                    })
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
