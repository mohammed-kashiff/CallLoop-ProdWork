import { useEffect, useMemo, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { CcEmpty, CcTable } from '../components/cc/CcTable'
import { ccErr, ccHint, ccInput, ccMono, ccRow, ccTd } from '../components/cc/classes'

// AC-63/AC-69: this org's own durable audit trail — who did what, when.
// Owner-or-manager only (AC-56), same tier as the ticket-audit team view
// and the rubric builder. Reads stay off this table entirely (AC-64's
// actor-tagged logs cover those) — every row here is a real
// state-changing action: a role change, a credential save, a rubric
// activation, an alias mapping, an export.

type AuditLogRow = {
  id: string
  actor_id: string | null
  actor_email: string | null
  action: string
  target_type: string | null
  target_id: string | null
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  ip_address: string | null
  created_at: string
}

function formatWhen(raw: string): string {
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function actionLabel(action: string): string {
  return action.replace(/[._]/g, ' ')
}

function diffSummary(row: AuditLogRow): string | null {
  if (!row.before && !row.after) return null
  const parts: string[] = []
  const keys = new Set([...Object.keys(row.before || {}), ...Object.keys(row.after || {})])
  for (const key of keys) {
    const from = row.before ? row.before[key] : undefined
    const to = row.after ? row.after[key] : undefined
    if (from === undefined && to !== undefined) parts.push(`${key}: ${JSON.stringify(to)}`)
    else if (from !== undefined && to === undefined) parts.push(`${key} removed`)
    else if (JSON.stringify(from) !== JSON.stringify(to)) {
      parts.push(`${key}: ${JSON.stringify(from)} → ${JSON.stringify(to)}`)
    }
  }
  return parts.length ? parts.join(', ') : null
}

export function ActivityLogTable({
  endpoint,
  showOrg,
  variant = 'default',
  filterable = false,
}: {
  endpoint: string
  showOrg?: boolean
  variant?: 'default' | 'cc'
  filterable?: boolean
}) {
  const [rows, setRows] = useState<(AuditLogRow & { org_id?: string })[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState('')

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch(endpoint)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load the activity log.'))
        return r.json() as Promise<{ rows: (AuditLogRow & { org_id?: string })[] }>
      })
      .then((data) => {
        if (!cancelled) setRows(data.rows)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load the activity log.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [endpoint])

  const visible = useMemo(() => {
    if (!rows) return []
    const q = filter.trim().toLowerCase()
    if (!q) return rows
    return rows.filter((row) => {
      const email = (row.actor_email || '').toLowerCase()
      const ip = (row.ip_address || '').toLowerCase()
      return email.includes(q) || ip.includes(q)
    })
  }, [rows, filter])

  if (loading) return <p className={variant === 'cc' ? ccHint : 'panel-lede'}>Loading…</p>
  if (error) {
    return (
      <p className={variant === 'cc' ? ccErr : 'upload-error'} role="alert">
        {error}
      </p>
    )
  }
  if (!rows || rows.length === 0) {
    if (variant === 'cc') {
      return (
        <CcEmpty
          title="No activity recorded yet"
          body="This fills in as real state-changing actions happen (a role change, a credential save, a rubric activation)."
        />
      )
    }
    return (
      <p className="empty-copy">
        No activity recorded yet — this fills in as real state-changing actions happen (a role
        change, a credential save, a rubric activation, an alias mapping, an export).
      </p>
    )
  }

  const table = variant === 'cc' ? (
    <CcTable
      columns={['When', ...(showOrg ? ['Org'] : []), 'Who', 'Action', 'Target', 'What changed']}
    >
      {visible.map((row) => (
        <tr key={row.id} className={ccRow}>
          <td className={ccTd}>{formatWhen(row.created_at)}</td>
          {showOrg ? <td className={`${ccTd} ${ccMono}`}>{row.org_id ? row.org_id.slice(0, 8) : '—'}</td> : null}
          <td className={ccTd}>{row.actor_email || (row.actor_id ? row.actor_id.slice(0, 8) : '—')}</td>
          <td className={ccTd}>{actionLabel(row.action)}</td>
          <td className={ccTd}>{row.target_id ? `${row.target_type || ''} ${row.target_id}`.trim() : '—'}</td>
          <td className={ccTd}>{diffSummary(row) || '—'}</td>
        </tr>
      ))}
    </CcTable>
  ) : (
    <div className="admin-table-wrap">
      <table className="admin-table">
        <thead>
          <tr>
            <th>When</th>
            {showOrg ? <th>Org</th> : null}
            <th>Who</th>
            <th>Action</th>
            <th>Target</th>
            <th>What changed</th>
          </tr>
        </thead>
        <tbody>
          {visible.map((row) => (
            <tr key={row.id}>
              <td>{formatWhen(row.created_at)}</td>
              {showOrg ? <td>{row.org_id ? row.org_id.slice(0, 8) : '—'}</td> : null}
              <td>{row.actor_email || (row.actor_id ? row.actor_id.slice(0, 8) : '—')}</td>
              <td>{actionLabel(row.action)}</td>
              <td>{row.target_id ? `${row.target_type || ''} ${row.target_id}`.trim() : '—'}</td>
              <td>{diffSummary(row) || '—'}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )

  return (
    <div className="grid gap-4">
      {filterable ? (
        <input
          type="search"
          className={variant === 'cc' ? ccInput : undefined}
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by email or IP"
          aria-label="Filter by email or IP"
        />
      ) : null}
      {visible.length === 0 ? (
        variant === 'cc' ? (
          <CcEmpty title="No matching rows" body="Try a different email or IP." />
        ) : (
          <p className="empty-copy">No matching rows.</p>
        )
      ) : (
        table
      )}
    </div>
  )
}

export function ActivityLog() {
  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop</p>
          <h1>Activity Log</h1>
        </div>
      </header>
      <p className="scaffold-banner">
        Every real state-changing action in this org — who did it, when, and what changed. Plain
        reads (viewing a ticket, browsing usage) aren't recorded here; this is the durable trail,
        not raw application logs.
      </p>
      <ActivityLogTable endpoint="/api/audit-log" />
    </>
  )
}
