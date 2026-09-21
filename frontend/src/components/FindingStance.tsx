import { useCallback, useEffect, useState } from 'react'
import { apiFetch, readError } from '../lib/api'

export type FindingStanceValue = 'agree' | 'dispute'

export type FindingStanceRow = {
  stance: FindingStanceValue
  note: string | null
  user_id?: string
  dimension_id?: string
  call_id?: number | null
  ticket_id?: string | null
}

type ApiRow = FindingStanceRow & { dimension_id: string }

export function useFindingResponses(opts: {
  channel: 'call' | 'ticket'
  callId?: number | null
  ticketId?: string | null
  enabled?: boolean
}) {
  const { channel, callId, ticketId, enabled = true } = opts
  const [rows, setRows] = useState<ApiRow[]>([])
  const [responses, setResponses] = useState<Record<string, FindingStanceRow>>({})
  const [canRespond, setCanRespond] = useState(false)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})

  const load = useCallback(() => {
    if (!enabled) return
    if (channel === 'call' && (callId == null || callId < 1)) return
    if (channel === 'ticket' && !ticketId) return
    const q = new URLSearchParams({ channel })
    if (channel === 'call' && callId != null) q.set('call_id', String(callId))
    if (channel === 'ticket' && ticketId) q.set('ticket_id', ticketId)
    apiFetch(`/api/findings/responses?${q}`)
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load responses.'))
        return r.json() as Promise<{ responses?: ApiRow[]; can_respond?: boolean }>
      })
      .then((body) => {
        const list = body.responses || []
        const next: Record<string, FindingStanceRow> = {}
        for (const row of list) {
          if (row.dimension_id) next[row.dimension_id] = row
        }
        setRows(list)
        setResponses(next)
        setCanRespond(Boolean(body.can_respond))
      })
      .catch(() => {
        setRows([])
        setResponses({})
        setCanRespond(false)
      })
  }, [channel, callId, ticketId, enabled])

  useEffect(() => {
    load()
  }, [load])

  const onRespond = useCallback(
    async (dimensionId: string, stance: FindingStanceValue, note?: string) => {
      setBusyId(dimensionId)
      setErrors((prev) => {
        const next = { ...prev }
        delete next[dimensionId]
        return next
      })
      try {
        const r = await apiFetch('/api/findings/response', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            channel,
            dimension_id: dimensionId,
            stance,
            note: note || null,
            call_id: channel === 'call' ? callId : null,
            ticket_id: channel === 'ticket' ? ticketId : null,
          }),
        })
        if (!r.ok) throw new Error(await readError(r, 'Could not save this response.'))
        const row = (await r.json()) as ApiRow
        setResponses((prev) => ({ ...prev, [dimensionId]: row }))
        setRows((prev) => {
          const rest = prev.filter(
            (item) => !(item.dimension_id === dimensionId && item.user_id === row.user_id),
          )
          return [...rest, row]
        })
        setCanRespond(true)
      } catch (e: unknown) {
        setErrors((prev) => ({
          ...prev,
          [dimensionId]: e instanceof Error ? e.message : 'Could not save this response.',
        }))
      } finally {
        setBusyId(null)
      }
    },
    [channel, callId, ticketId],
  )

  return { responses, rows, canRespond, busyId, errors, onRespond }
}

export function FindingStance({
  current,
  canRespond,
  busy,
  error,
  onRespond,
}: {
  current?: FindingStanceRow | null
  canRespond: boolean
  busy?: boolean
  error?: string | null
  onRespond: (stance: FindingStanceValue, note?: string) => void
}) {
  const [disputeOpen, setDisputeOpen] = useState(false)
  const [note, setNote] = useState(current?.stance === 'dispute' ? current.note || '' : '')

  if (!canRespond && !current) return null

  if (!canRespond && current) {
    return (
      <p className="finding-stance-status">
        {current.stance === 'agree' ? 'Agreed' : `Disputed${current.note ? `: ${current.note}` : ''}`}
      </p>
    )
  }

  return (
    <div className="finding-stance">
      <div className="finding-stance-actions">
        <button
          type="button"
          className={['ghost-btn', current?.stance === 'agree' ? 'is-current' : ''].filter(Boolean).join(' ')}
          disabled={busy}
          onClick={() => {
            setDisputeOpen(false)
            onRespond('agree')
          }}
        >
          Agree
        </button>
        <button
          type="button"
          className={['ghost-btn', current?.stance === 'dispute' ? 'is-current' : ''].filter(Boolean).join(' ')}
          disabled={busy}
          onClick={() => setDisputeOpen(true)}
        >
          Dispute
        </button>
      </div>
      {disputeOpen ? (
        <label className="finding-stance-note">
          <span className="visually-hidden">Why you dispute this finding</span>
          <textarea
            className="train-reply"
            value={note}
            maxLength={400}
            rows={2}
            placeholder="A short note is required"
            onChange={(e) => setNote(e.target.value)}
          />
          <button
            type="button"
            className="start-btn"
            disabled={busy || !note.trim()}
            onClick={() => onRespond('dispute', note.trim())}
          >
            {busy ? 'Saving…' : 'Submit dispute'}
          </button>
        </label>
      ) : null}
      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}
      {current && !disputeOpen ? (
        <p className="finding-stance-status">
          {current.stance === 'agree' ? 'You agreed.' : `You disputed${current.note ? `: ${current.note}` : '.'}`}
        </p>
      ) : null}
    </div>
  )
}
