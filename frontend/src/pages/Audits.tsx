import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { capFirst } from '../lib/format'
import { apiFetch, readError } from '../lib/api'
import type { CallListItem } from '../types'

const PAGE_SIZES = [5, 10, 25] as const
type FlagFilter = 'all' | 'flagged' | 'unflagged'

function formatWhen(raw: string | null): string {
  if (!raw) return '—'
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw.replace('T', ' ').slice(0, 16)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function sourceLabel(source: string | null | undefined): string {
  const s = (source || '').toLowerCase()
  if (s === 'justcall') return 'JustCall'
  if (!s || s === 'upload') return 'Manual'
  return capFirst(s)
}

function churnChip(risk: string | null | undefined): { text: string; className: string } {
  const raw = String(risk || 'none').toLowerCase()
  const tone = raw === 'high' || raw === 'medium' || raw === 'low' ? raw : 'none'
  return {
    text: capFirst(tone === 'none' ? 'None' : tone),
    className: `call-picker-chip is-label is-churn-${tone}`,
  }
}

export function Audits() {
  const navigate = useNavigate()
  const [calls, setCalls] = useState<CallListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [flagFilter, setFlagFilter] = useState<FlagFilter>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(10)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch('/api/calls')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load calls.'))
        return r.json() as Promise<CallListItem[]>
      })
      .then((rows) => {
        if (cancelled) return
        const list = Array.isArray(rows) ? rows : []
        list.sort((a, b) => {
          const ta = new Date(a.created_at || 0).getTime()
          const tb = new Date(b.created_at || 0).getTime()
          if (tb !== ta) return tb - ta
          return b.id - a.id
        })
        setCalls(list)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load calls.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const filtered = useMemo(() => {
    if (flagFilter === 'flagged') return calls.filter((c) => c.flagged)
    if (flagFilter === 'unflagged') return calls.filter((c) => !c.flagged)
    return calls
  }, [calls, flagFilter])

  const pages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage = Math.min(page, pages)
  const start = (safePage - 1) * pageSize
  const slice = filtered.slice(start, start + pageSize)
  const rangeEnd = start + slice.length

  const openCall = (id: number) => {
    navigate(`/audits/${id}`)
  }

  const deleteCall = async (id: number) => {
    if (!window.confirm('Delete this call? The recording will be removed and this cannot be undone.')) {
      return
    }
    setDeleteError(null)
    setDeletingId(id)
    try {
      const r = await apiFetch(`/api/calls/${id}`, { method: 'DELETE' })
      if (!r.ok) throw new Error(await readError(r, 'Could not delete this call.'))
      setCalls((prev) => prev.filter((c) => c.id !== id))
    } catch (e: unknown) {
      setDeleteError(e instanceof Error ? e.message : 'Could not delete this call.')
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop</p>
          <h1>Audits</h1>
        </div>
      </header>

      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {deleteError ? (
        <p className="upload-error" role="alert">
          {deleteError}
        </p>
      ) : null}
      {loading ? <p className="panel-lede">Loading audits…</p> : null}

      {!loading && !error ? (
        <>
          <div className="audit-toolbar">
            <div className="audit-filters" role="group" aria-label="Flagged filter">
              {(['all', 'flagged', 'unflagged'] as const).map((key) => (
                <button
                  key={key}
                  type="button"
                  className={['ghost-btn', flagFilter === key ? 'is-current' : '']
                    .filter(Boolean)
                    .join(' ')}
                  aria-pressed={flagFilter === key}
                  onClick={() => {
                    setFlagFilter(key)
                    setPage(1)
                  }}
                >
                  {key === 'all' ? 'All' : key === 'flagged' ? 'Flagged' : 'Unflagged'}
                </button>
              ))}
            </div>
          </div>

          {filtered.length ? (
            <>
              <div className="admin-table-wrap">
                <table className="admin-table audit-table">
                  <thead>
                    <tr>
                      <th>Date</th>
                      <th>Call</th>
                      <th>Score</th>
                      <th>Grade</th>
                      <th>Churn</th>
                      <th>Flagged</th>
                      <th>Source</th>
                      <th aria-label="Actions" />
                    </tr>
                  </thead>
                  <tbody>
                    {slice.map((row) => {
                      const chip = churnChip(row.churn_risk)
                      return (
                        <tr
                          key={row.id}
                          className="audit-row"
                          tabIndex={0}
                          onClick={() => openCall(row.id)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              openCall(row.id)
                            }
                          }}
                        >
                          <td>{formatWhen(row.created_at)}</td>
                          <td>
                            <span className="admin-org">{capFirst(row.filename)}</span>
                            <span className="admin-id">#{row.id}</span>
                          </td>
                          <td>{row.score != null ? row.score : '—'}</td>
                          <td>{row.grade || '—'}</td>
                          <td>
                            <span className={chip.className}>{chip.text}</span>
                          </td>
                          <td>{row.flagged ? 'Flagged' : '—'}</td>
                          <td>{sourceLabel(row.source)}</td>
                          <td>
                            <button
                              type="button"
                              className="audit-delete-btn"
                              aria-label={`Delete call #${row.id}`}
                              disabled={deletingId === row.id}
                              onClick={(e) => {
                                e.stopPropagation()
                                void deleteCall(row.id)
                              }}
                            >
                              {deletingId === row.id ? '…' : '×'}
                            </button>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
              <div className="review-pager">
                <label className="review-page-size">
                  <span>Items per page</span>
                  <select
                    value={pageSize}
                    onChange={(e) => {
                      setPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number])
                      setPage(1)
                    }}
                  >
                    {PAGE_SIZES.map((size) => (
                      <option key={size} value={size}>
                        {size}
                      </option>
                    ))}
                  </select>
                </label>
                <p className="review-pager-status">
                  {start + 1}–{rangeEnd} of {filtered.length}
                </p>
                <nav className="review-page-nav" aria-label="Audit pages">
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={safePage <= 1}
                    onClick={() => setPage(safePage - 1)}
                  >
                    Previous
                  </button>
                  {Array.from({ length: pages }, (_, i) => i + 1).map((n) => (
                    <button
                      key={n}
                      type="button"
                      className={['ghost-btn', 'is-page', n === safePage ? 'is-current' : '']
                        .filter(Boolean)
                        .join(' ')}
                      aria-current={n === safePage ? 'page' : undefined}
                      onClick={() => setPage(n)}
                    >
                      {n}
                    </button>
                  ))}
                  <button
                    type="button"
                    className="ghost-btn"
                    disabled={safePage >= pages}
                    onClick={() => setPage(safePage + 1)}
                  >
                    Next
                  </button>
                </nav>
              </div>
            </>
          ) : (
            <p className="empty-copy">
              {flagFilter === 'flagged'
                ? 'No flagged calls.'
                : flagFilter === 'unflagged'
                  ? 'No unflagged calls.'
                  : 'No calls in this workspace yet.'}
            </p>
          )}
        </>
      ) : null}
    </>
  )
}
