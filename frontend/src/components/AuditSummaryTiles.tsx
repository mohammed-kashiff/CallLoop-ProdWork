import { TicketEvidence } from './TicketEvidence'

type Highlight = {
  id?: string | null
  name?: string | null
  reasoning?: string | null
  evidence_text?: string | null
  evidence_seq?: number | null
  evidence_verified?: boolean | null
} | null

type ResolvedEvidence = { isImage: boolean; assetUrl?: string }

export function AuditSummaryTiles({
  summary,
  strength,
  gap,
  resolveEvidence,
}: {
  summary?: string | null
  strength?: Highlight
  gap?: Highlight
  resolveEvidence?: (seq: number | null | undefined) => ResolvedEvidence | undefined
}) {
  if (!summary && !strength && !gap) return null

  function tile(label: string, h: Highlight | undefined) {
    const resolved = resolveEvidence?.(h?.evidence_seq)
    return (
      <div>
        <dt>{label}</dt>
        <dd>{(h?.name || '').trim() || '—'}</dd>
        {h?.reasoning ? <p className="score-summary-reason">{h.reasoning}</p> : null}
        <TicketEvidence
          text={h?.evidence_text ?? null}
          isImage={Boolean(resolved?.isImage)}
          assetUrl={resolved?.assetUrl}
          verified={h?.evidence_verified ?? undefined}
        />
      </div>
    )
  }

  return (
    <div className="score-summary">
      {summary ? <p className="score-summary-lede">{summary}</p> : null}
      <dl className="score-summary-tiles">
        {tile('Top strength', strength)}
        {tile('Top gap', gap)}
      </dl>
    </div>
  )
}
