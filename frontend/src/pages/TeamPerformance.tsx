import { useCallback, useEffect, useMemo, useState } from 'react'
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
type KpiChannel = 'tickets' | 'calls'

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
  target?: number | null
  met?: boolean | null
}

type HeatmapGrid = {
  dimensions: Array<{ id: string; name: string }>
  rows: Array<{ user_id: string; cells: HeatmapCell[] }>
}

type Snapshot = {
  view_scope: 'team' | 'own'
  days: number
  org: {
    tickets: { avg_score: number | null; count: number; total: number; target?: number | null }
    calls: { avg_score: number | null; count: number; total: number; target?: number | null }
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

type KpiOverride = { user_id: string; display_name: string; target: number }
type KpiDim = {
  id: string
  name: string
  org_target: number | null
  overrides: KpiOverride[]
}
type KpiCatalog = {
  tickets: { dimensions: KpiDim[] }
  calls: { dimensions: KpiDim[] }
  roster: Array<{ user_id: string; display_name: string }>
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

function fmtTarget(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

function highlightName(h: Highlight): string {
  const name = (h?.name || '').trim()
  return name || '—'
}

function scoreTone(n: number | null, target?: number | null): 'default' | 'good' | 'warn' | 'bad' {
  if (n == null) return 'default'
  if (target != null && Number.isFinite(target)) {
    if (n >= target) return 'good'
    if (n >= Math.max(0, target - 20)) return 'warn'
    return 'bad'
  }
  if (n >= 80) return 'good'
  if (n >= 60) return 'warn'
  return 'bad'
}

function coverageHint(count: number, total: number, target?: number | null): string {
  const cov = total > 0 ? `${count} scored / ${total}` : `${count} scored`
  if (target == null || !Number.isFinite(target)) return cov
  return `${cov} · target ${fmtTarget(target)}`
}

function heatmapTone(cell: HeatmapCell): 'default' | 'good' | 'warn' | 'bad' {
  if (cell.rate == null) return 'default'
  return scoreTone(cell.rate * 100, cell.target)
}

function fmtPassRate(cell: HeatmapCell): string {
  if (cell.n <= 0) return '—'
  return `${cell.pass}/${cell.n}`
}

function heatmapTitle(cell: HeatmapCell): string {
  const base = `${cell.name}: ${fmtPassRate(cell)}`
  if (cell.target == null || !Number.isFinite(cell.target)) return base
  return `${base} (target ${fmtTarget(cell.target)})`
}

function putChannel(channel: KpiChannel): 'ticket' | 'call' {
  return channel === 'tickets' ? 'ticket' : 'call'
}

function cmpNum(a: number | null, b: number | null, dir: SortDir): number {
  const av = a == null ? -1 : a
  const bv = b == null ? -1 : b
  return dir === 'asc' ? av - bv : bv - av
}

function TargetInput({
  value,
  onSave,
  ariaLabel,
  placeholder,
}: {
  value: number | null
  onSave: (next: number | null) => Promise<void>
  ariaLabel: string
  placeholder?: string
}) {
  const [draft, setDraft] = useState(value == null ? '' : String(value))
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    setDraft(value == null ? '' : String(value))
  }, [value])

  const commit = async () => {
    const trimmed = draft.trim()
    const next = trimmed === '' ? null : Number(trimmed)
    if (next != null && (!Number.isFinite(next) || next < 0 || next > 100)) {
      setDraft(value == null ? '' : String(value))
      return
    }
    if (next === value || (next == null && value == null)) return
    setBusy(true)
    try {
      await onSave(next)
    } catch {
      setDraft(value == null ? '' : String(value))
    } finally {
      setBusy(false)
    }
  }

  return (
    <input
      type="number"
      min={0}
      max={100}
      step={1}
      className="team-perf-kpi-input"
      value={draft}
      disabled={busy}
      aria-label={ariaLabel}
      placeholder={placeholder || '—'}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={() => void commit()}
      onKeyDown={(e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          ;(e.currentTarget as HTMLInputElement).blur()
        }
      }}
    />
  )
}

function KpiPanel({
  catalog,
  canEdit,
  userId,
  onSave,
  error,
}: {
  catalog: KpiCatalog
  canEdit: boolean
  userId: string | null
  onSave: (channel: 'ticket' | 'call', dimensionId: string, agentUserId: string | null, target: number | null) => Promise<void>
  error: string | null
}) {
  const [channel, setChannel] = useState<KpiChannel>('tickets')
  const [openIds, setOpenIds] = useState<Set<string>>(new Set())
  const dims = channel === 'tickets' ? catalog.tickets.dimensions : catalog.calls.dimensions
  const roster = catalog.roster

  const toggle = (id: string) => {
    setOpenIds((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <section className="team-perf-panel" aria-label={canEdit ? 'Set KPIs' : 'Your KPI targets'}>
      <div className="team-perf-heat-head">
        <h2>{canEdit ? 'Set KPIs' : 'Your KPI targets'}</h2>
        <div className="audit-filters" role="group" aria-label="KPI channel">
          <button
            type="button"
            className={['ghost-btn', channel === 'tickets' ? 'is-current' : ''].filter(Boolean).join(' ')}
            aria-pressed={channel === 'tickets'}
            onClick={() => setChannel('tickets')}
          >
            Tickets
          </button>
          <button
            type="button"
            className={['ghost-btn', channel === 'calls' ? 'is-current' : ''].filter(Boolean).join(' ')}
            aria-pressed={channel === 'calls'}
            onClick={() => setChannel('calls')}
          >
            Calls
          </button>
        </div>
      </div>
      <p className="panel-lede">
        {canEdit
          ? 'Org default applies to everyone. Expand a criterion to set per-agent overrides. New rubric criteria show up here automatically.'
          : 'Pass-rate target for each live criterion. An override you have been given beats the org default.'}
      </p>
      {error ? <p className="upload-error" role="alert">{error}</p> : null}
      {dims.length === 0 ? (
        <p className="empty-copy">No live criteria on the active rubric yet.</p>
      ) : (
        <div className="admin-table-wrap">
          <table className="admin-table team-perf-kpi">
            <thead>
              <tr>
                <th>Criterion</th>
                <th>{canEdit ? 'Org target' : 'Target'}</th>
                {canEdit ? <th>Agents</th> : <th>Source</th>}
              </tr>
            </thead>
            <tbody>
              {dims.map((dim) => {
                const mine = dim.overrides.find((o) => o.user_id === userId)
                const effective = mine?.target ?? dim.org_target
                const source = mine
                  ? 'your target'
                  : dim.org_target != null
                    ? 'org default'
                    : 'not set'
                const open = openIds.has(dim.id)
                return (
                  <tr key={dim.id}>
                    <td>{dim.name}</td>
                    <td>
                      {canEdit ? (
                        <TargetInput
                          value={dim.org_target}
                          ariaLabel={`${dim.name} org target`}
                          onSave={(next) => onSave(putChannel(channel), dim.id, null, next)}
                        />
                      ) : (
                        effective == null ? '—' : fmtTarget(effective)
                      )}
                    </td>
                    <td>
                      {canEdit ? (
                        <>
                          <button
                            type="button"
                            className="team-perf-kpi-expand"
                            aria-expanded={open}
                            onClick={() => toggle(dim.id)}
                          >
                            {open ? 'Hide agents' : `Set per agent (${roster.length})`}
                          </button>
                          {open ? (
                            <div className="team-perf-kpi-agents">
                              {roster.length === 0 ? (
                                <p className="empty-copy">No teammates to assign.</p>
                              ) : (
                                roster.map((m) => {
                                  const ov = dim.overrides.find((o) => o.user_id === m.user_id)
                                  return (
                                    <div key={m.user_id} className="team-perf-kpi-agent">
                                      <span>{m.display_name}</span>
                                      <span className="team-perf-kpi-agent-ctrl">
                                        {ov == null && dim.org_target != null ? (
                                          <span className="team-perf-kpi-inherit">
                                            inherits {fmtTarget(dim.org_target)}
                                          </span>
                                        ) : null}
                                        <TargetInput
                                          value={ov?.target ?? null}
                                          placeholder={dim.org_target == null ? '—' : fmtTarget(dim.org_target)}
                                          ariaLabel={`${dim.name} target for ${m.display_name}`}
                                          onSave={(next) => onSave(putChannel(channel), dim.id, m.user_id, next)}
                                        />
                                      </span>
                                    </div>
                                  )
                                })
                              )}
                            </div>
                          ) : (
                            <span className="team-perf-kpi-meta">
                              {dim.overrides.length === 0
                                ? 'org default'
                                : `${dim.overrides.length} override${dim.overrides.length === 1 ? '' : 's'}`}
                            </span>
                          )}
                        </>
                      ) : (
                        <span className="team-perf-kpi-meta">{source}</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

export function TeamPerformance() {
  const { isOwnerOrManager, userId, role } = useAuth()
  const [days, setDays] = useState<Days>(30)
  const [data, setData] = useState<Snapshot | null>(null)
  const [kpis, setKpis] = useState<KpiCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kpiError, setKpiError] = useState<string | null>(null)
  const [sortKey, setSortKey] = useState<SortKey>('name')
  const [sortDir, setSortDir] = useState<SortDir>('asc')
  const [heatChannel, setHeatChannel] = useState<KpiChannel>('tickets')

  const loadSnapshot = useCallback(() => {
    return apiFetch(`/api/team-performance?days=${days}`).then(async (r) => {
      if (!r.ok) throw new Error(await readError(r, 'Could not load team performance.'))
      return r.json() as Promise<Snapshot>
    })
  }, [days])

  const loadKpis = useCallback(() => {
    return apiFetch('/api/performance-kpis').then(async (r) => {
      if (!r.ok) throw new Error(await readError(r, 'Could not load KPI targets.'))
      return r.json() as Promise<KpiCatalog>
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    setKpiError(null)
    loadSnapshot()
      .then((body) => {
        if (!cancelled) setData(body)
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Could not load team performance.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    loadKpis()
      .then((catalog) => {
        if (!cancelled) setKpis(catalog)
      })
      .catch((err: Error) => {
        if (!cancelled) setKpiError(err.message || 'Could not load KPI targets.')
      })
    return () => {
      cancelled = true
    }
  }, [days, loadSnapshot, loadKpis])

  const saveKpi = useCallback(
    async (
      channel: 'ticket' | 'call',
      dimensionId: string,
      agentUserId: string | null,
      target: number | null,
    ) => {
      setKpiError(null)
      const r = await apiFetch('/api/performance-kpis', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          channel,
          dimension_id: dimensionId,
          agent_user_id: agentUserId,
          target,
        }),
      })
      if (!r.ok) {
        const msg = await readError(r, 'Could not save KPI.')
        setKpiError(msg)
        throw new Error(msg)
      }
      const [catalog, snap] = await Promise.all([loadKpis(), loadSnapshot()])
      setKpis(catalog)
      setData(snap)
    },
    [loadKpis, loadSnapshot],
  )

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
  const ticketTarget = data?.org.tickets.target ?? null
  const callTarget = data?.org.calls.target ?? null
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
              hint={coverageHint(data.org.tickets.count, data.org.tickets.total, ticketTarget)}
              tone={scoreTone(ticketAvg, ticketTarget)}
            />
            <KpiCard
              label="Call avg"
              value={fmtScore(callAvg)}
              hint={coverageHint(data.org.calls.count, data.org.calls.total, callTarget)}
              tone={scoreTone(callAvg, callTarget)}
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

          {kpis ? (
            <KpiPanel
              catalog={kpis}
              canEdit={isOwnerOrManager}
              userId={userId}
              onSave={saveKpi}
              error={kpiError}
            />
          ) : kpiError ? (
            <p className="upload-error" role="alert">{kpiError}</p>
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
              When a KPI is set, the cell colors against that target instead of 80 / 60.
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
                            const tone = heatmapTone(cell)
                            const channel = heatChannel === 'tickets' ? 'ticket' : 'call'
                            const canPractice = cell.n > 0 && Boolean(row.user_id)
                            const href = `/training?channel=${channel}&dim=${encodeURIComponent(cell.id)}&agent=${encodeURIComponent(row.user_id)}&days=${days}`
                            return (
                              <td key={cell.id} className={`team-perf-heat-cell tone-${tone}`}>
                                {canPractice ? (
                                  <Link to={href} title={heatmapTitle(cell)}>
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
