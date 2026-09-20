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
    <ol className="grid gap-0 border-l border-cc-line pl-5">
      {ordered.map((e, i) => (
        <li key={`${e.stage}-${e.created_at}-${i}`} className="relative pb-6 last:pb-0">
          <span
            className={`absolute -left-[1.55rem] flex h-6 w-6 items-center justify-center rounded-full border text-[11px] font-semibold ${
              e.status === 'failed'
                ? 'border-cc-fail bg-cc-card text-cc-fail'
                : e.status === 'succeeded'
                  ? 'border-cc-pass bg-cc-card text-cc-pass'
                  : 'border-cc-line bg-cc-card text-cc-muted'
            }`}
            aria-hidden="true"
          >
            {statusMark(e.status)}
          </span>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <p className="text-sm font-semibold text-cc-ink">{e.stage}</p>
            <p className="text-[12px] text-cc-muted">
              {e.created_at ? new Date(e.created_at).toLocaleString() : '—'}
            </p>
          </div>
          {e.agentId ? <p className={`${ccMono} mt-1`}>Agent {e.agentId}</p> : null}
          {e.error ? <p className="mt-1 text-[13px] text-cc-fail">{e.error}</p> : null}
          {e.apis && e.apis.length > 0 ? (
            <ul className="mt-2 grid gap-1">
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
          {e.extra && Object.keys(e.extra).length > 0 ? (
            <pre className="mt-2 overflow-x-auto rounded-md bg-cc-paper p-3 font-mono text-[12px] text-cc-muted">
              {JSON.stringify(e.extra, null, 2)}
            </pre>
          ) : null}
        </li>
      ))}
    </ol>
  )
}
