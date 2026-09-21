import { ccMono } from './classes'
import { isReconstructed, orderTrailEvents, type CcTrailEvent } from './trailStats'

export type { CcTrailEvent }

function statusMark(status: CcTrailEvent['status']): string {
  if (status === 'succeeded') return '✓'
  if (status === 'failed') return '✕'
  return '…'
}

export function CcTimeline({
  events,
  selectedIndex = 0,
  onSelect,
}: {
  events: CcTrailEvent[]
  selectedIndex?: number
  onSelect?: (index: number) => void
}) {
  if (events.length === 0) {
    return (
      <p className="rounded-lg border border-dashed border-cc-line px-6 py-10 text-center text-[16px] text-cc-muted">
        No pipeline events recorded yet.
      </p>
    )
  }

  const ordered = orderTrailEvents(events)

  return (
    <ol className="border-l-2 border-cc-accent/40 pl-6">
      {ordered.map((e, i) => {
        const selected = i === selectedIndex
        return (
          <li key={`${e.stage}-${e.created_at}-${i}`} className="relative pb-3 last:pb-0">
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
            <button
              type="button"
              className={`w-full rounded-lg px-3 py-2 text-left ${
                selected ? 'bg-cc-wash' : 'hover:bg-cc-paper'
              }`}
              aria-current={selected ? 'true' : undefined}
              onClick={() => onSelect?.(i)}
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
                <p className="text-[17px] font-bold text-cc-navy">{e.stage}</p>
                <p className="text-[14px] text-cc-muted">
                  {e.created_at ? new Date(e.created_at).toLocaleString() : '—'}
                </p>
                {isReconstructed(e.extra) ? (
                  <span className="rounded bg-cc-paper px-1.5 py-0.5 text-[12px] font-semibold text-cc-muted">
                    Reconstructed
                  </span>
                ) : null}
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
            </button>
          </li>
        )
      })}
    </ol>
  )
}
