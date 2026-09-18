import { useCallback, useEffect, useState } from 'react'
import { useAuth } from '../context/AuthContext'
import { apiFetch, readError } from '../lib/api'

// Owner/manager set org-default and per-agent pass-rate targets here;
// a member gets the same page but a read-only view of the org default
// plus whatever override they've been given — never a teammate's.
// Access is enforced server-side (PUT /api/performance-kpis is
// owner-or-manager only, GET already scopes a member's own row) — the
// canEdit split below is presentation only, matching RubricBuilder.tsx's
// own pattern of one page whose controls adapt by role rather than two
// separate routes.

type KpiChannel = 'tickets' | 'calls'

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

function fmtTarget(n: number): string {
  return Number.isInteger(n) ? String(n) : n.toFixed(1)
}

function putChannel(channel: KpiChannel): 'ticket' | 'call' {
  return channel === 'tickets' ? 'ticket' : 'call'
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

export function KpiTargets() {
  const { isOwnerOrManager, userId } = useAuth()
  const [catalog, setCatalog] = useState<KpiCatalog | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    return apiFetch('/api/performance-kpis').then(async (r) => {
      if (!r.ok) throw new Error(await readError(r, 'Could not load KPI targets.'))
      return r.json() as Promise<KpiCatalog>
    })
  }, [])

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    load()
      .then((body) => {
        if (!cancelled) setCatalog(body)
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message || 'Could not load KPI targets.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [load])

  const saveKpi = useCallback(
    async (
      channel: 'ticket' | 'call',
      dimensionId: string,
      agentUserId: string | null,
      target: number | null,
    ) => {
      setError(null)
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
        setError(msg)
        throw new Error(msg)
      }
      setCatalog(await load())
    },
    [load],
  )

  return (
    <div>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Profile</p>
          <h1>{isOwnerOrManager ? 'KPI targets' : 'Your KPI targets'}</h1>
        </div>
      </header>

      {loading && !catalog ? <p className="panel-lede">Loading KPI targets…</p> : null}
      {error && !catalog ? <p className="upload-error" role="alert">{error}</p> : null}

      {catalog ? (
        <KpiPanel
          catalog={catalog}
          canEdit={isOwnerOrManager}
          userId={userId}
          onSave={saveKpi}
          error={error}
        />
      ) : null}
    </div>
  )
}
