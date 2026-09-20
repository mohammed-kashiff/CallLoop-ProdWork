import { ccMono } from './classes'

export type CcTrailEvent = {
  stage: string
  status: 'started' | 'succeeded' | 'failed'
  created_at: string | null
  error?: string | null
  apis?: { method: string; endpoint: string }[]
  extra?: Record<string, unknown> | null
  agentId?: string | null
}

function statusMark(status: CcTrailEvent['status']): string {
  if (status === 'succeeded') return '✓'
  if (status === 'failed') return '✕'
  return '…'
}

function extraEntries(extra: Record<string, unknown>): [string, string][] {
  const out: [string, string][] = []
  for (const [key, value] of Object.entries(extra)) {
    if (value == null || value === '') continue
    const text = typeof value === 'string' ? value : JSON.stringify(value)
    if (!text || text === '{}' || text === '[]') continue
    out.push([key, text])
  }
  return out
}

export function CcTimeline({ events }: { events: CcTrailEvent[] }) {
  if (events.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-cc-line px-6 py-10 text-center text-sm text-cc-muted">
        No pipeline events recorded yet.
      </p>
    )
  }

  const ordered = [
    ...events.filter((e) => e.status === 'failed'),
    ...events.filter((e) => e.status !== 'failed'),
  ]

  return (
    <ol className="max-w-2xl border-l border-cc-line pl-5">
      {ordered.map((e, i) => {
        const extra = e.extra ? extraEntries(e.extra) : []
        return (
          <li key={`${e.stage}-${e.created_at}-${i}`} className="relative pb-5 last:pb-0">
            <span
              className={`absolute -left-[1.55rem] flex h-6 w-6 items-center justify-center rounded-full border bg-cc-card text-[11px] font-semibold ${
                e.status === 'failed'
                  ? 'border-cc-fail text-cc-fail'
                  : e.status === 'succeeded'
                    ? 'border-cc-pass text-cc-pass'
                    : 'border-cc-line text-cc-muted'
              }`}
              aria-hidden="true"
            >
              {statusMark(e.status)}
            </span>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
              <p className="text-sm font-semibold text-cc-ink">{e.stage}</p>
              <p className="text-[12px] text-cc-muted">
                {e.created_at ? new Date(e.created_at).toLocaleString() : '—'}
              </p>
            </div>
            {e.agentId ? <p className={`${ccMono} mt-0.5`}>Agent {e.agentId}</p> : null}
            {e.error ? <p className="mt-1 text-[13px] text-cc-fail">{e.error}</p> : null}
            {e.apis && e.apis.length > 0 ? (
              <ul className="mt-1.5 grid gap-0.5">
                {e.apis.map((api, j) => (
                  <li key={`${api.method}-${api.endpoint}-${j}`} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-[11px] font-semibold tracking-wide text-cc-muted">
                      {api.method}
                    </span>
                    <code className={ccMono}>{api.endpoint}</code>
                  </li>
                ))}
              </ul>
            ) : e.apis ? (
              <p className="mt-1 text-[12px] text-cc-muted">No API recorded for this step</p>
            ) : null}
            {extra.length > 0 ? (
              <details className="mt-1.5">
                <summary className="cursor-pointer text-[12px] font-semibold text-cc-muted">
                  Details
                </summary>
                <dl className="mt-2 grid gap-2">
                  {extra.map(([key, text]) => (
                    <div key={key}>
                      <dt className="text-[11px] font-semibold uppercase tracking-wide text-cc-muted">
                        {key}
                      </dt>
                      <dd className="break-words text-[13px] text-cc-ink">{text}</dd>
                    </div>
                  ))}
                </dl>
              </details>
            ) : null}
          </li>
        )
      })}
    </ol>
  )
}
