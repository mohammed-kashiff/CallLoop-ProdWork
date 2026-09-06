import { useEffect, useState } from 'react'
import { apiFetch, readError } from '../lib/api'
import { useAuth } from '../context/AuthContext'
import { flagEnabled } from '../lib/features'

// Read-only rubric viewer, opened from the account menu in a new tab.
// Shows whatever rubric is currently active per engine — not editable
// here; editing calls' rubric stays on /rubric-builder (owner-only).

type RubricDimension = {
  id: string
  name: string | null
  weight: number
  question?: string | null
}

type CallRubric = {
  source: 'custom' | 'legacy'
  name: string | null
  version: number | null
  dimensions: RubricDimension[]
}

type TicketRubric = {
  name: string | null
  version: number | null
  dimensions: RubricDimension[]
}

function DimensionList({ dimensions }: { dimensions: RubricDimension[] }) {
  const totalWeight = dimensions.reduce((sum, d) => sum + (d.weight || 0), 0)
  return (
    <ul className="rubric-view-list">
      {dimensions.map((d, i) => (
        <li key={`${d.id}-${i}`} className="rubric-view-item">
          <div className="rubric-view-item-top">
            <h3>{d.name || d.id}</h3>
            <span className="rubric-view-weight">{d.weight}%</span>
          </div>
          {d.question ? <p className="rubric-view-question">{d.question}</p> : null}
        </li>
      ))}
      <li className="rubric-view-total">Total weight: {totalWeight}%</li>
    </ul>
  )
}

export function RubricView() {
  const { features } = useAuth()
  const ticketAuditEnabled = flagEnabled(features, 'show_ticket_audit_nav')

  const [callRubric, setCallRubric] = useState<CallRubric | null>(null)
  const [callError, setCallError] = useState<string | null>(null)
  const [ticketRubric, setTicketRubric] = useState<TicketRubric | null>(null)
  const [ticketError, setTicketError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    const tasks: Promise<void>[] = [
      apiFetch('/api/rubric')
        .then(async (r) => {
          if (!r.ok) throw new Error(await readError(r, 'Could not load the call rubric.'))
          return r.json() as Promise<CallRubric>
        })
        .then((data) => {
          if (!cancelled) setCallRubric(data)
        })
        .catch((e: unknown) => {
          if (!cancelled) setCallError(e instanceof Error ? e.message : 'Could not load the call rubric.')
        }),
    ]
    if (ticketAuditEnabled) {
      tasks.push(
        apiFetch('/api/tickets/rubric')
          .then(async (r) => {
            if (!r.ok) throw new Error(await readError(r, 'Could not load the ticket rubric.'))
            return r.json() as Promise<TicketRubric>
          })
          .then((data) => {
            if (!cancelled) setTicketRubric(data)
          })
          .catch((e: unknown) => {
            if (!cancelled) setTicketError(e instanceof Error ? e.message : 'Could not load the ticket rubric.')
          }),
      )
    }
    Promise.all(tasks).finally(() => {
      if (!cancelled) setLoading(false)
    })
    return () => {
      cancelled = true
    }
  }, [ticketAuditEnabled])

  return (
    <>
      <header className="page-bar">
        <div>
          <p className="crumb">Loop</p>
          <h1>Rubric</h1>
        </div>
      </header>

      <p className="scaffold-banner">
        View only — this is whatever rubric is currently active and being audited against. To
        change the call rubric, use Rubric builder.
      </p>

      {loading ? <p className="panel-lede">Loading…</p> : null}

      {!loading ? (
        <section aria-label="Rubric for calls" className="admin-card">
          <h2 className="panel-title">Rubric — Calls</h2>
          {callError ? (
            <p className="upload-error" role="alert">
              {callError}
            </p>
          ) : callRubric ? (
            <>
              <p className="panel-lede">
                {callRubric.name || 'Default rubric'}
                {callRubric.version ? ` · v${callRubric.version}` : ''}
              </p>
              <DimensionList dimensions={callRubric.dimensions} />
            </>
          ) : null}
        </section>
      ) : null}

      {!loading && ticketAuditEnabled ? (
        <section aria-label="Rubric for tickets" className="admin-card">
          <h2 className="panel-title">Rubric — Tickets</h2>
          {ticketError ? (
            <p className="upload-error" role="alert">
              {ticketError}
            </p>
          ) : ticketRubric ? (
            <>
              <p className="panel-lede">
                {ticketRubric.name || 'Ticket QA'}
                {ticketRubric.version ? ` · v${ticketRubric.version}` : ''}
              </p>
              <DimensionList dimensions={ticketRubric.dimensions} />
            </>
          ) : null}
        </section>
      ) : null}
    </>
  )
}
