import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { apiFetch, readError } from '../lib/api'
import { capFirst } from '../lib/format'

// TA-10 (PRD §3/§9/§10): its own page, not a variant of AuditDetail.tsx —
// the ticket engine is a separate engine from calls end to end.

type TicketMessage = {
  seq: number
  speaker: string
  text: string
  agent_user_id: string | null
  sent_at: string | null
  has_image: boolean
}

type TicketAsset = {
  seq: number
  width: number
  height: number
  content_type: string
}

type TicketFinding = {
  id: string
  name?: string
  verdict: string
  reasoning?: string
  evidence_text: string | null
  evidence_seq: number | null
  evidence_verified: boolean
  attributed_to: string | null
  weight?: number
  // TA-13: Response Timeliness is computed from real message timestamps,
  // not judged by Claude — not folded into the weighted score above.
  deterministic?: boolean
}

type TicketAuditResult = {
  score: number
  primary_owner: string | null
  spans: Array<{
    agent_user_id: string | null
    start_seq: number
    end_seq: number
    turn_count: number
  }>
  findings: TicketFinding[]
  created_at?: string
  view_scope?: 'full' | 'own'
}

type TicketDetail = {
  id: string
  source: string
  status: string
  created_at: string | null
  messages: TicketMessage[]
  assets: TicketAsset[]
  audit: TicketAuditResult | null
  view_scope?: 'full' | 'own'
}

function verdictSlug(verdict: string): string {
  if (verdict === 'not_applicable') return 'n-a'
  if (verdict === 'error') return 'fail'
  return verdict
}

function verdictLabel(verdict: string): string {
  if (verdict === 'not_applicable') return 'N/A'
  return verdict.toUpperCase()
}

export function TicketAudit() {
  const { ticketId } = useParams()
  const navigate = useNavigate()
  const inputRef = useRef<HTMLInputElement>(null)

  const [ticket, setTicket] = useState<TicketDetail | null>(null)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [scoring, setScoring] = useState(false)
  const [scoreError, setScoreError] = useState<string | null>(null)
  const [assetUrls, setAssetUrls] = useState<Record<number, string>>({})

  const loadTicket = useCallback(async (id: string) => {
    setLoading(true)
    setLoadError(null)
    try {
      const r = await apiFetch(`/api/tickets/${id}`)
      if (!r.ok) throw new Error(await readError(r, 'Could not load this ticket.'))
      const data = (await r.json()) as TicketDetail
      setTicket(data)
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : 'Could not load this ticket.')
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    setAssetUrls({})
    if (ticketId) void loadTicket(ticketId)
    else setTicket(null)
  }, [ticketId, loadTicket])

  const uploadFile = useCallback(
    async (file: File) => {
      setUploading(true)
      setUploadError(null)
      try {
        const fd = new FormData()
        fd.append('file', file)
        const r = await apiFetch('/api/tickets/upload', { method: 'POST', body: fd })
        if (!r.ok) throw new Error(await readError(r, 'Upload failed.'))
        const data = (await r.json()) as { ticket_id: string }
        navigate(`/ticket-audit/${data.ticket_id}`)
      } catch (e) {
        setUploadError(e instanceof Error ? e.message : 'Upload failed.')
      } finally {
        setUploading(false)
      }
    },
    [navigate],
  )

  const onDrop = (e: DragEvent) => {
    e.preventDefault()
    setDragging(false)
    if (uploading) return
    const file = e.dataTransfer.files?.[0]
    if (file) void uploadFile(file)
  }

  const onChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    e.target.value = ''
    if (file) void uploadFile(file)
  }

  const runScore = useCallback(async () => {
    if (!ticketId) return
    setScoring(true)
    setScoreError(null)
    try {
      const r = await apiFetch(`/api/tickets/${ticketId}/score`, { method: 'POST' })
      if (!r.ok) throw new Error(await readError(r, 'Scoring failed.'))
      const result = (await r.json()) as TicketAuditResult
      setTicket((prev) => (prev ? { ...prev, audit: result } : prev))
    } catch (e) {
      setScoreError(e instanceof Error ? e.message : 'Scoring failed.')
    } finally {
      setScoring(false)
    }
  }, [ticketId])

  const loadAssetUrl = useCallback(async (id: string, seq: number) => {
    try {
      const r = await apiFetch(`/api/tickets/${id}/assets/${seq}`)
      if (!r.ok) return
      const data = (await r.json()) as { url: string }
      setAssetUrls((prev) => (prev[seq] ? prev : { ...prev, [seq]: data.url }))
    } catch {
      // best-effort — a missing screenshot just doesn't render
    }
  }, [])

  useEffect(() => {
    if (!ticket) return
    for (const m of ticket.messages) {
      if (m.has_image) void loadAssetUrl(ticket.id, m.seq)
    }
  }, [ticket, loadAssetUrl])

  const messagesBySeq = new Map((ticket?.messages || []).map((m) => [m.seq, m]))

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/ticket-audit">Ticket Audit</Link>
            {ticket ? ` / #${ticket.id.slice(0, 8)}` : ''}
          </p>
          <h1>Ticket Audit</h1>
        </div>
      </header>

      <p className="scaffold-banner">
        Scaffolding — six placeholder criteria (not the final rubric, see TA-13) exercising the
        pipeline end to end. Scores here validate the mechanism, not a real performance review.
      </p>

      {!ticketId ? (
        <section
          className={[
            'upload-panel',
            dragging ? 'is-dragging' : '',
            uploading ? 'is-disabled' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          aria-label="Upload a ticket PDF"
          onDragEnter={(e) => {
            e.preventDefault()
            if (!uploading) setDragging(true)
          }}
          onDragOver={(e) => e.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <input
            ref={inputRef}
            type="file"
            accept="application/pdf"
            hidden
            disabled={uploading}
            onChange={onChange}
          />
          <div className="upload-copy">
            <p className="panel-lede">
              {uploading ? 'Uploading…' : 'Drop a JustCall ticket PDF export here, or'}
            </p>
          </div>
          <div className="upload-actions">
            <button
              type="button"
              className="choose-btn"
              disabled={uploading}
              onClick={() => inputRef.current?.click()}
            >
              Choose file
            </button>
          </div>
          {uploadError && (
            <p className="upload-error" role="alert">
              {uploadError}
            </p>
          )}
        </section>
      ) : null}

      {loading ? <p className="panel-lede">Loading ticket…</p> : null}
      {loadError ? (
        <p className="upload-error" role="alert">
          {loadError}
        </p>
      ) : null}

      {ticket && ticket.view_scope === 'own' ? (
        <p className="scaffold-banner">
          Showing only your own contribution to this ticket (TA-12) — not another agent's
          turns or scores, even though this thread is shared.
        </p>
      ) : null}

      {ticket ? (
        <div className="eval-split">
          <div className="eval-pane">
            <section aria-label="Scorecard">
              <h2 className="panel-title">Scorecard</h2>
              {ticket.status !== 'ready' ? (
                <p className="panel-lede">
                  {ticket.status === 'failed'
                    ? "This ticket's PDF could not be ingested, so there is nothing to score."
                    : "This ticket is still processing — scoring isn't available yet."}
                </p>
              ) : !ticket.audit ? (
                <>
                  <p className="panel-lede">Not scored yet.</p>
                  <button
                    type="button"
                    className="start-btn"
                    disabled={scoring}
                    onClick={() => void runScore()}
                  >
                    {scoring ? 'Scoring…' : 'Score this ticket'}
                  </button>
                  {scoreError && (
                    <p className="upload-error" role="alert">
                      {scoreError}
                    </p>
                  )}
                </>
              ) : (
                <>
                  <p className="score-headline">
                    {Math.round(ticket.audit.score)}
                    <span>/100</span>
                  </p>
                  <ul className="criteria-list">
                    {ticket.audit.findings.map((f) => {
                      const turn =
                        f.evidence_seq != null ? messagesBySeq.get(f.evidence_seq) : undefined
                      const assetUrl = turn?.has_image ? assetUrls[turn.seq] : undefined
                      return (
                        <li key={f.id} className="criterion">
                          <div className="criterion-top">
                            <h3>
                              {f.name || f.id}
                              {f.deterministic && (
                                <span className="nav-soon" title="Computed from real message timestamps, not judged by Claude">
                                  Measured
                                </span>
                              )}
                            </h3>
                            <span className={`verdict verdict-${verdictSlug(f.verdict)}`}>
                              {verdictLabel(f.verdict)}
                            </span>
                          </div>
                          {f.reasoning && <p className="criterion-rationale">{f.reasoning}</p>}
                          {assetUrl ? (
                            <figure className="evidence is-image">
                              <img src={assetUrl} alt="Evidence screenshot" />
                              {f.evidence_text && <figcaption>{f.evidence_text}</figcaption>}
                            </figure>
                          ) : f.evidence_text ? (
                            <blockquote className="evidence">
                              <p>&ldquo;{f.evidence_text}&rdquo;</p>
                              {!f.evidence_verified && (
                                <span className="evidence-unverified">
                                  Quote not verified verbatim
                                </span>
                              )}
                            </blockquote>
                          ) : null}
                        </li>
                      )
                    })}
                  </ul>
                </>
              )}
            </section>
          </div>
          <div className="eval-pane is-transcript">
            <h2 className="panel-title">Ticket thread</h2>
            {/* TA-12: the backend already filtered this list server-side —
                the full thread for a manager (org owner), or only the
                viewer's own span for anyone else. Nothing to filter here. */}
            <ul className="ticket-thread">
              {ticket.messages.map((m) => (
                <li key={m.seq} className={`ticket-turn is-${m.speaker}`}>
                  <span className="ticket-turn-speaker">{capFirst(m.speaker)}</span>
                  {m.has_image && assetUrls[m.seq] ? (
                    <img className="ticket-turn-image" src={assetUrls[m.seq]} alt="Screenshot" />
                  ) : null}
                  <p>{m.text}</p>
                </li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}
    </>
  )
}
