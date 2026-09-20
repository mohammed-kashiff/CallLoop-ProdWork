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
      <p className="rounded-lg border border-dashed border-cc-line px-6 py-10 text-center text-[16px] text-cc-muted">
        No pipeline events recorded yet.
      </p>
    )
  }

  const ordered = [
    ...events.filter((e) => e.status === 'failed'),
    ...events.filter((e) => e.status !== 'failed'),
  ]

  return (
    <ol className="max-w-2xl border-l-2 border-cc-accent/40 pl-6">
      {ordered.map((e, i) => {
        const extra = e.extra ? extraEntries(e.extra) : []
        return (
          <li key={`${e.stage}-${e.created_at}-${i}`} className="relative pb-6 last:pb-0">
            <span
              className={`absolute -left-[1.7rem] flex h-7 w-7 items-center justify-center rounded-full border-2 text-[13px] font-bold ${
                e.status === 'failed'
                  ? 'border-cc-fail bg-cc-fail text-white'
                  : e.status === 'succeeded'
                    ? 'border-cc-accent bg-cc-accent text-white'
                    : 'border-cc-line bg-cc-card text-cc-muted'
              }`}
              aria-hidden="true"
            >
              {statusMark(e.status)}
            </span>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
              <p className="text-[17px] font-bold text-cc-navy">{e.stage}</p>
              <p className="text-[14px] text-cc-muted">
                {e.created_at ? new Date(e.created_at).toLocaleString() : '—'}
              </p>
            </div>
            {e.agentId ? <p className={`${ccMono} mt-0.5`}>Agent {e.agentId}</p> : null}
            {e.error ? <p className="mt-1 text-[15px] text-cc-fail">{e.error}</p> : null}
            {e.apis && e.apis.length > 0 ? (
              <ul className="mt-1.5 grid gap-0.5">
                {e.apis.map((api, j) => (
                  <li key={`${api.method}-${api.endpoint}-${j}`} className="flex flex-wrap items-baseline gap-2">
                    <span className="rounded bg-cc-wash px-1.5 py-0.5 text-[13px] font-bold text-cc-accent">
                      {api.method}
                    </span>
                    <code className={ccMono}>{api.endpoint}</code>
                  </li>
                ))}
              </ul>
            ) : e.apis ? (
              <p className="mt-1 text-[14px] text-cc-muted">No API recorded for this step</p>
            ) : null}
            {extra.length > 0 ? (
              <details className="mt-1.5">
                <summary className="cursor-pointer text-[15px] font-semibold text-cc-accent">
                  Details
                </summary>
                <dl className="mt-2 grid gap-2">
                  {extra.map(([key, text]) => (
                    <div key={key}>
                      <dt className="text-[13px] font-semibold text-cc-muted">
                        {key}
                      </dt>
                      <dd className="break-words text-[15px] text-cc-ink">{text}</dd>
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
