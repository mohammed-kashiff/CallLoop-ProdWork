import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { apiFetch, readError } from '../lib/api'
import { capFirst } from '../lib/format'

// TA-12 (PRD §7): an agent's own contribution rolled up across every
// ticket they've touched — never another agent's turns or scores, even
// on a thread they share. The backend does all the filtering; this page
// just renders what GET /api/tickets/mine already scoped down.

type OwnTurn = {
  seq: number
  speaker: string
  text: string
  has_image: boolean
}

type OwnFinding = {
  id: string
  name?: string
  verdict: string
  evidence_text: string | null
}

type OwnTicketContribution = {
  ticket_id: string
  status: string
  created_at: string | null
  turns: OwnTurn[]
  findings: OwnFinding[] | null
}

function verdictSlug(verdict: string): string {
  if (verdict === 'not_applicable') return 'n-a'
  if (verdict === 'error') return 'fail'
  return verdict
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
        Scaffolding — your own contribution across every ticket you've touched, never another
        agent's turns or scores even on a shared thread (TA-12).
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
              <li key={m.seq} className={`ticket-turn is-${m.speaker}`}>
                <span className="ticket-turn-speaker">{capFirst(m.speaker)}</span>
                <p>{m.text}</p>
              </li>
            ))}
          </ul>
          {t.findings && t.findings.length > 0 ? (
            <ul className="criteria-list">
              {t.findings.map((f) => (
                <li key={f.id} className="criterion">
                  <div className="criterion-top">
                    <h3>{f.name || f.id}</h3>
                    <span className={`verdict verdict-${verdictSlug(f.verdict)}`}>
                      {f.verdict === 'not_applicable' ? 'N/A' : f.verdict.toUpperCase()}
                    </span>
                  </div>
                  {f.evidence_text && (
                    <blockquote className="evidence">
                      <p>&ldquo;{f.evidence_text}&rdquo;</p>
                    </blockquote>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="panel-lede">Not scored yet, or none of the scored criteria were yours.</p>
          )}
        </section>
      ))}
    </>
  )
}
