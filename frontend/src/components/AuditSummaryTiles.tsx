type Highlight = { id?: string | null; name?: string | null } | null

export function AuditSummaryTiles({
  summary,
  strength,
  gap,
}: {
  summary?: string | null
  strength?: Highlight
  gap?: Highlight
}) {
  if (!summary && !strength && !gap) return null
  return (
    <div className="score-summary">
      {summary ? <p className="score-summary-lede">{summary}</p> : null}
      <dl className="score-summary-tiles">
        <div>
          <dt>Top strength</dt>
          <dd>{(strength?.name || '').trim() || '—'}</dd>
        </div>
        <div>
          <dt>Top gap</dt>
          <dd>{(gap?.name || '').trim() || '—'}</dd>
        </div>
      </dl>
    </div>
  )
}
