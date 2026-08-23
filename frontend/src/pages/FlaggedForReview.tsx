import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Workspace } from '../components/Workspace'
import { useAudit } from '../context/AuditContext'
import { apiFetch, readError } from '../lib/api'
import { capFirst, capWords } from '../lib/format'
import type { FlaggedCallRow } from '../types'

const PAGE_SIZES = [5, 10, 25] as const
type ReviewStatus = 'pending' | 'completed'

interface FlagItem {
  id: string
  callId: number
  fileName: string
  agentName: string
  score: number
  reason: string
  flaggedAt: string
  status: ReviewStatus
  note?: string
}

function formatWhen(raw: string | null): string {
  if (!raw) return ''
  const d = new Date(raw)
  if (Number.isNaN(d.getTime())) return raw.replace('T', ' ').slice(0, 16)
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function fromRow(row: FlaggedCallRow): FlagItem {
  return {
    id: String(row.id),
    callId: row.id,
    fileName: row.filename,
    agentName: row.agent_name || 'Agent',
    score: Number(row.score) || 0,
    reason: row.reasons || 'Flagged',
    flaggedAt: formatWhen(row.audited_at || row.created_at),
    status: row.solved ? 'completed' : 'pending',
  }
}

function NoteIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path
        fill="currentColor"
        d="M7 3.5h7.2L19 8.3V20a1.5 1.5 0 0 1-1.5 1.5h-10A1.5 1.5 0 0 1 6 20V5A1.5 1.5 0 0 1 7.5 3.5H7Z"
        opacity="0.22"
      />
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
        d="M14.2 3.5H7.5A1.5 1.5 0 0 0 6 5v15a1.5 1.5 0 0 0 1.5 1.5h10A1.5 1.5 0 0 0 19 20V8.3Z"
      />
      <path fill="none" stroke="currentColor" strokeWidth="1.6" d="M14.2 3.5V8.3H19" />
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        d="M9 12.2h6M9 15.4h6M9 18.6h3.8"
      />
    </svg>
  )
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <circle cx="12" cy="12" r="8.2" fill="none" stroke="currentColor" strokeWidth="1.6" />
      <path
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m8.4 12.2 2.5 2.5 4.8-5.2"
      />
    </svg>
  )
}

function ReviewRow({
  item,
  noteOpen,
  onToggleNote,
  onNote,
  onComplete,
  onOpen,
}: {
  item: FlagItem
  noteOpen: boolean
  onToggleNote: () => void
  onNote: (note: string) => void
  onComplete?: () => void
  onOpen: () => void
}) {
  return (
    <li className="review-item">
      <div className="review-main">
        <p className="review-file">{capFirst(item.fileName)}</p>
        <p className="review-meta">
          {capWords(item.agentName)} · {item.flaggedAt}
        </p>
        {noteOpen ? (
          <label className="review-note">
            <span className="visually-hidden">Note for {item.fileName}</span>
            <textarea
              className="review-note-field"
              value={item.note ?? ''}
              maxLength={8000}
              placeholder="Add a note…"
              onChange={(e) => onNote(e.target.value)}
            />
          </label>
        ) : item.note ? (
          <p className="review-note-preview">{item.note}</p>
        ) : null}
      </div>
      <div className="review-aside">
        <span className="review-score">{item.score}</span>
        <span className="review-reason">{item.reason}</span>
        <div className="review-actions">
          <button
            type="button"
            className={['review-action', 'is-note', noteOpen || item.note ? 'is-on' : '']
              .filter(Boolean)
              .join(' ')}
            aria-label={item.note ? 'Edit note' : 'Add note'}
            aria-pressed={noteOpen}
            onClick={onToggleNote}
          >
            <NoteIcon />
          </button>
          {onComplete ? (
            <button
              type="button"
              className="review-action is-check"
              aria-label="Mark review completed"
              onClick={onComplete}
            >
              <CheckIcon />
            </button>
          ) : null}
          <button type="button" className="ghost-btn" onClick={onOpen}>
            Open
          </button>
        </div>
      </div>
    </li>
  )
}

function ReviewPage({
  items,
  status,
  noteOpenId,
  onToggleNote,
  onNote,
  onComplete,
  onOpen,
}: {
  items: FlagItem[]
  status: ReviewStatus
  noteOpenId: string | null
  onToggleNote: (id: string) => void
  onNote: (id: string, note: string) => void
  onComplete?: (id: string) => void
  onOpen: (item: FlagItem) => void
}) {
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState<(typeof PAGE_SIZES)[number]>(5)
  const pages = Math.max(1, Math.ceil(items.length / pageSize))
  const safePage = Math.min(page, pages)
  const start = (safePage - 1) * pageSize
  const slice = items.slice(start, start + pageSize)
  const rangeEnd = start + slice.length

  return (
    <div className={`review-lane is-${status === 'pending' ? 'pending' : 'done'}`}>
      {items.length ? (
        <>
          <ul className="review-list">
            {slice.map((item) => (
              <ReviewRow
                key={item.id}
                item={item}
                noteOpen={noteOpenId === item.id}
                onToggleNote={() => onToggleNote(item.id)}
                onNote={(note) => onNote(item.id, note)}
                onComplete={onComplete ? () => onComplete(item.id) : undefined}
                onOpen={() => onOpen(item)}
              />
            ))}
          </ul>
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
              {start + 1}–{rangeEnd} of {items.length}
            </p>
            <nav className="review-page-nav" aria-label="Review pages">
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
        <p className="review-empty">
          {status === 'pending' ? 'Nothing waiting for review.' : 'No completed reviews yet.'}
        </p>
      )}
    </div>
  )
}

export function FlaggedForReview() {
  const navigate = useNavigate()
  const { selectCall } = useAudit()
  const [items, setItems] = useState<FlagItem[]>([])
  const [tab, setTab] = useState<ReviewStatus>('pending')
  const [noteOpenId, setNoteOpenId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    apiFetch('/api/calls/flagged')
      .then((r) => {
        if (!r.ok) throw new Error('Could not load flagged calls.')
        return r.json() as Promise<FlaggedCallRow[]>
      })
      .then((rows) => {
        if (!cancelled) setItems(Array.isArray(rows) ? rows.map(fromRow) : [])
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load flagged calls.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  const pending = useMemo(() => items.filter((item) => item.status === 'pending'), [items])
  const completed = useMemo(() => items.filter((item) => item.status === 'completed'), [items])

  const patch = (id: string, next: Partial<FlagItem>) => {
    setItems((list) => list.map((item) => (item.id === id ? { ...item, ...next } : item)))
  }

  const openCall = (item: FlagItem) => {
    void selectCall(item.callId).then(() => navigate('/agents-pulse'))
  }

  const shared = {
    noteOpenId,
    onToggleNote: (id: string) => setNoteOpenId((open) => (open === id ? null : id)),
    onNote: (id: string, note: string) => patch(id, { note }),
    onOpen: openCall,
  }

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop / Agent Pulse</p>
          <h1>Flagged for review</h1>
        </div>
      </header>

      {error && (
        <p className="upload-error" role="alert">
          {error}
        </p>
      )}
      {loading && <p className="panel-lede">Loading flagged calls…</p>}

      <Workspace
        allowNotes={false}
        activeId={tab}
        onActiveId={(id) => setTab(id as ReviewStatus)}
        tabs={[
          {
            id: 'pending',
            label: `Review pending · ${pending.length}`,
            panel: (
              <ReviewPage
                items={pending}
                status="pending"
                onComplete={(id) => {
                  const row = items.find((i) => i.id === id)
                  if (!row) return
                  void (async () => {
                    const r = await apiFetch(`/api/calls/${row.callId}/solve`, {
                      method: 'POST',
                    })
                    if (!r.ok) {
                      setError(await readError(r, 'Could not solve this review.'))
                      return
                    }
                    patch(id, { status: 'completed' })
                    setNoteOpenId((open) => (open === id ? null : open))
                    setTab('completed')
                  })()
                }}
                {...shared}
              />
            ),
          },
          {
            id: 'completed',
            label: `Review completed · ${completed.length}`,
            panel: <ReviewPage items={completed} status="completed" {...shared} />,
          },
        ]}
      />
    </>
  )
}
