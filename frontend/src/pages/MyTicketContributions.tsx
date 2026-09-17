import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch, readError } from '../lib/api'
import { AuditSummaryTiles } from '../components/AuditSummaryTiles'
import { TicketEvidence } from '../components/TicketEvidence'
import { capFirst } from '../lib/format'

// TA-12/TA-21/TA-30 (PRD §7): an agent's own scorecard rolled up across
// every ticket they've touched — never a teammate's individual score, even
// on a thread they share. Unlike the old behavior, the full thread comes
// back for every ticket (TA-30): own_span_seqs marks which turns are the
// viewer's own to highlight, rather than the backend hiding the rest.

type OwnTurn = {
  seq: number
  speaker: string
  text: string
  display_name: string | null
  has_image: boolean
}

type OwnFinding = {
  id: string
  name?: string
  verdict: string
  evidence_text: string | null
  evidence_seq: number | null
  weight?: number
  earned?: number | null
}

type OwnTicketContribution = {
  ticket_id: string
  status: string
  created_at: string | null
  turns: OwnTurn[]
  own_span_seqs: number[]
  findings: OwnFinding[] | null
  top_strength?: { id?: string | null; name?: string | null } | null
  top_gap?: { id?: string | null; name?: string | null } | null
  audit_summary?: string | null
}

function verdictSlug(verdict: string): string {
  if (verdict === 'not_applicable') return 'n-a'
  if (verdict === 'error') return 'fail'
  return verdict
}

function marksLabel(f: OwnFinding): string | null {
  if (f.earned == null || f.weight == null) return null
  const fmt = (n: number) => (Number.isInteger(n) ? String(n) : n.toFixed(1))
  return `${fmt(f.earned)}/${fmt(f.weight)}`
}

export function MyTicketContributions() {
  const [tickets, setTickets] = useState<OwnTicketContribution[] | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    apiFetch('/api/tickets/mine')
      .then(async (r) => {
        if (!r.ok) throw new Error(await readError(r, 'Could not load your ticket contributions.'))
        return r.json() as Promise<{ tickets: OwnTicketContribution[] }>
      })
      .then((data) => {
        if (!cancelled) setTickets(data.tickets)
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : 'Could not load this page.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">
            <Link to="/ticket-audit">Ticket Audit</Link> / My contributions
          </p>
          <h1>My Ticket Contributions</h1>
        </div>
      </header>

      <p className="scaffold-banner">
        Your own scorecard across every ticket you've touched — never a teammate's individual
        score, even on a shared thread. The full thread is shown for each ticket; your own turns
        are highlighted.
      </p>

      {loading ? <p className="panel-lede">Loading…</p> : null}
      {error ? (
        <p className="upload-error" role="alert">
          {error}
        </p>
      ) : null}

      {!loading && !error && tickets && tickets.length === 0 ? (
        <p className="empty-copy">
          No tickets found where you're identified as the agent yet — today's PDF ingestion
          can't always resolve who replied, so this may be empty even on tickets you worked.
        </p>
      ) : null}

      {tickets?.map((t) => (
        <section key={t.ticket_id} className="admin-card" aria-label={`Ticket ${t.ticket_id}`}>
          <p className="admin-id">
            <Link to={`/ticket-audit/${t.ticket_id}`}>#{t.ticket_id.slice(0, 8)}</Link>
            {' · '}
            {capFirst(t.status)}
          </p>
          <ul className="ticket-thread">
            {t.turns.map((m) => (
              <li
                key={m.seq}
                className={[
                  `ticket-turn is-${m.speaker}`,
                  t.own_span_seqs.includes(m.seq) ? 'is-own-turn' : '',
                ]
                  .filter(Boolean)
                  .join(' ')}
              >
                <span className="ticket-turn-speaker">
                  {capFirst(m.speaker)}
                  {m.display_name ? ` (${m.display_name})` : ''}
                </span>
                <p>{m.text}</p>
              </li>
            ))}
          </ul>
          {t.findings && t.findings.length > 0 ? (
            <>
              <AuditSummaryTiles
                summary={t.audit_summary}
                strength={t.top_strength}
                gap={t.top_gap}
              />
              <ul className="criteria-list">
              {t.findings.map((f) => (
                <li key={f.id} className="criterion">
                  <div className="criterion-top">
                    <h3>{f.name || f.id}</h3>
                    <span className="criterion-badges">
                      {marksLabel(f) && <span className="criterion-marks">{marksLabel(f)}</span>}
                      <span className={`verdict verdict-${verdictSlug(f.verdict)}`}>
                        {f.verdict === 'not_applicable' ? 'N/A' : f.verdict.toUpperCase()}
                      </span>
                    </span>
                  </div>
                  <TicketEvidence
                    text={f.evidence_text}
                    isImage={
                      f.evidence_seq != null &&
                      t.turns.some((turn) => turn.seq === f.evidence_seq && turn.has_image)
                    }
                  />
                </li>
              ))}
              </ul>
            </>
          ) : (
            <p className="panel-lede">Not scored yet, or none of the scored criteria were yours.</p>
          )}
        </section>
      ))}
    </>
  )
}
