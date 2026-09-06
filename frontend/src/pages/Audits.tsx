import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { capFirst } from '../lib/format'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import { flagEnabled } from '../lib/features'
import type { CallListItem } from '../types'

const PAGE_SIZES = [5, 10, 25] as const
type FlagFilter = 'all' | 'flagged' | 'unflagged'
type Engine = 'calls' | 'tickets'

type TicketListItem = {
  id: string
  source: string
  status: string
  created_at: string | null
  message_count: number
  has_audit: boolean
  score: number | null
}

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
  const { features } = useAuth()
  const ticketAuditEnabled = flagEnabled(features, 'show_ticket_audit_nav')

  const [engine, setEngine] = useState<Engine>('calls')

  const [calls, setCalls] = useState<CallListItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [flagFilter, setFlagFilter] = useState<FlagFilter>('all')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(10)
  const [deletingId, setDeletingId] = useState<number | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  const [tickets, setTickets] = useState<TicketListItem[]>([])
  const [ticketsLoading, setTicketsLoading] = useState(false)
  const [ticketsError, setTicketsError] = useState<string | null>(null)
  const [ticketPage, setTicketPage] = useState(1)
  const [ticketPageSize, setTicketPageSize] = useState<(typeof PAGE_SIZES)[number]>(10)

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

  useEffect(() => {
    if (!ticketAuditEnabled) return
    let cancelled = false
    setTicketsLoading(true)
    apiFetch('/api/tickets')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load tickets.'))
        return r.json() as Promise<{ tickets: TicketListItem[] }>
      })
      .then((data) => {
        if (!cancelled) setTickets(data.tickets || [])
      })
      .catch((e: unknown) => {
        if (!cancelled) setTicketsError(e instanceof Error ? e.message : 'Could not load tickets.')
      })
      .finally(() => {
        if (!cancelled) setTicketsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [ticketAuditEnabled])

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

  const ticketPages = Math.max(1, Math.ceil(tickets.length / ticketPageSize))
  const ticketSafePage = Math.min(ticketPage, ticketPages)
  const ticketStart = (ticketSafePage - 1) * ticketPageSize
  const ticketSlice = tickets.slice(ticketStart, ticketStart + ticketPageSize)
  const ticketRangeEnd = ticketStart + ticketSlice.length

  const openCall = (id: number) => {
    navigate(`/audits/${id}`)
  }

  const openTicket = (id: string) => {
    navigate(`/ticket-audit/${id}`)
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

      {ticketAuditEnabled ? (
        <div className="audit-filters" role="group" aria-label="Audit history for">
          {(['calls', 'tickets'] as const).map((key) => (
            <button
              key={key}
              type="button"
              className={['ghost-btn', engine === key ? 'is-current' : ''].filter(Boolean).join(' ')}
              aria-pressed={engine === key}
              onClick={() => setEngine(key)}
            >
              {key === 'calls' ? 'Calls' : 'Tickets'}
            </button>
          ))}
        </div>
      ) : null}

      {engine === 'calls' ? (
        <>
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
      ) : null}

      {engine === 'tickets' ? (
        <>
          {ticketsError ? (
            <p className="upload-error" role="alert">
              {ticketsError}
            </p>
          ) : null}
          {ticketsLoading ? <p className="panel-lede">Loading audits…</p> : null}

          {!ticketsLoading && !ticketsError ? (
            tickets.length ? (
              <>
                <div className="admin-table-wrap">
                  <table className="admin-table audit-table">
                    <thead>
                      <tr>
                        <th>Date</th>
                        <th>Ticket</th>
                        <th>Status</th>
                        <th>Score</th>
                        <th>Messages</th>
                        <th>Source</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ticketSlice.map((row) => (
                        <tr
                          key={row.id}
                          className="audit-row"
                          tabIndex={0}
                          onClick={() => openTicket(row.id)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              openTicket(row.id)
                            }
                          }}
                        >
                          <td>{formatWhen(row.created_at)}</td>
                          <td>
                            <span className="admin-id">#{row.id.slice(0, 8)}</span>
                          </td>
                          <td>{capFirst(row.status)}</td>
                          <td>{row.has_audit && row.score != null ? Math.round(row.score) : '—'}</td>
                          <td>{row.message_count}</td>
                          <td>{sourceLabel(row.source)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="review-pager">
                  <label className="review-page-size">
                    <span>Items per page</span>
                    <select
                      value={ticketPageSize}
                      onChange={(e) => {
                        setTicketPageSize(Number(e.target.value) as (typeof PAGE_SIZES)[number])
                        setTicketPage(1)
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
                    {ticketStart + 1}–{ticketRangeEnd} of {tickets.length}
                  </p>
                  <nav className="review-page-nav" aria-label="Ticket audit pages">
                    <button
                      type="button"
                      className="ghost-btn"
                      disabled={ticketSafePage <= 1}
                      onClick={() => setTicketPage(ticketSafePage - 1)}
                    >
                      Previous
                    </button>
                    {Array.from({ length: ticketPages }, (_, i) => i + 1).map((n) => (
                      <button
                        key={n}
                        type="button"
                        className={['ghost-btn', 'is-page', n === ticketSafePage ? 'is-current' : '']
                          .filter(Boolean)
                          .join(' ')}
                        aria-current={n === ticketSafePage ? 'page' : undefined}
                        onClick={() => setTicketPage(n)}
                      >
                        {n}
                      </button>
                    ))}
                    <button
                      type="button"
                      className="ghost-btn"
                      disabled={ticketSafePage >= ticketPages}
                      onClick={() => setTicketPage(ticketSafePage + 1)}
                    >
                      Next
                    </button>
                  </nav>
                </div>
              </>
            ) : (
              <p className="empty-copy">No tickets in this workspace yet.</p>
            )
          ) : null}
        </>
      ) : null}
    </>
  )
}
