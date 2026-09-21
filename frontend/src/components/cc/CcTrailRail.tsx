import { Link } from 'react-router-dom'
import { ccHint, ccMono } from './classes'
import {
  isReconstructed,
  trailCounts,
  trailDetailEntries,
  trailSpan,
  type CcTrailEvent,
} from './trailStats'

export function CcTrailRail({
  events,
  selectedIndex,
  orgId,
  logsTo,
  identity,
  source,
}: {
  events: CcTrailEvent[]
  selectedIndex: number
  orgId: string
  logsTo: string
  identity: string
  source?: string | null
}) {
  const counts = trailCounts(events)
  const span = trailSpan(events)
  const selected = events[selectedIndex] ?? null
  const details = selected ? trailDetailEntries(selected.extra) : []

  return (
    <div className="grid gap-4 rounded-xl border border-cc-line bg-cc-card p-5 shadow-sm">
      <div>
        <p className="text-[13px] font-semibold text-cc-muted">Pipeline</p>
        <p className="mt-1 text-[16px] font-semibold text-cc-navy">{identity}</p>
        {source ? <p className={`${ccHint} mt-0.5`}>{source}</p> : null}
        {events.some((e) => isReconstructed(e.extra)) ? (
          <p className={`${ccHint} mt-1`}>
            Reconstructed from the stored scorecard — not a live pipeline log.
          </p>
        ) : null}
      </div>

      <dl className="grid grid-cols-3 gap-2 text-center">
        <div className="rounded-lg bg-cc-wash px-2 py-2">
          <dt className="text-[12px] font-semibold text-cc-muted">Passed</dt>
          <dd className="text-[18px] font-bold text-cc-pass">{counts.succeeded}</dd>
        </div>
        <div className="rounded-lg bg-cc-paper px-2 py-2">
          <dt className="text-[12px] font-semibold text-cc-muted">Failed</dt>
          <dd className="text-[18px] font-bold text-cc-fail">{counts.failed}</dd>
        </div>
        <div className="rounded-lg bg-cc-paper px-2 py-2">
          <dt className="text-[12px] font-semibold text-cc-muted">Open</dt>
          <dd className="text-[18px] font-bold text-cc-navy">{counts.started}</dd>
        </div>
      </dl>

      <dl className="grid gap-2 text-[14px]">
        <div>
          <dt className="text-[12px] font-semibold text-cc-muted">Started</dt>
          <dd className="text-cc-ink">{span.first || '—'}</dd>
        </div>
        <div>
          <dt className="text-[12px] font-semibold text-cc-muted">Last step</dt>
          <dd className="text-cc-ink">{span.last || '—'}</dd>
        </div>
        <div>
          <dt className="text-[12px] font-semibold text-cc-muted">Elapsed</dt>
          <dd className="text-cc-ink">{span.elapsed || '—'}</dd>
        </div>
        <div>
          <dt className="text-[12px] font-semibold text-cc-muted">Org</dt>
          <dd>
            <Link className={`${ccMono} text-cc-accent hover:underline`} to={logsTo}>
              {orgId}
            </Link>
          </dd>
        </div>
      </dl>

      {selected ? (
        <div className="border-t border-cc-line pt-4">
          <p className="text-[13px] font-semibold text-cc-muted">Selected step</p>
          <p className="mt-1 text-[16px] font-bold text-cc-navy">{selected.stage}</p>
          {isReconstructed(selected.extra) ? (
            <p className={`${ccHint} mt-0.5`}>Reconstructed</p>
          ) : null}
          {selected.agentId ? (
            <p className={`${ccMono} mt-0.5`}>Agent {selected.agentId}</p>
          ) : null}
          {selected.error ? (
            <p className="mt-2 text-[14px] text-cc-fail">{selected.error}</p>
          ) : null}
          {selected.apis && selected.apis.length > 0 ? (
            <ul className="mt-2 grid gap-1">
              {selected.apis.map((api, j) => (
                <li key={`${api.method}-${api.endpoint}-${j}`} className="flex flex-wrap items-baseline gap-2">
                  <span className="rounded bg-cc-wash px-1.5 py-0.5 text-[12px] font-bold text-cc-accent">
                    {api.method}
                  </span>
                  <code className={ccMono}>{api.endpoint}</code>
                </li>
              ))}
            </ul>
          ) : selected.apis ? (
            <p className="mt-2 text-[14px] text-cc-muted">No API recorded for this step</p>
          ) : null}
          {details.length > 0 ? (
            <dl className="mt-3 grid gap-2">
              {details.map(([key, text]) => (
                <div key={key}>
                  <dt className="text-[12px] font-semibold text-cc-muted">{key}</dt>
                  <dd className="break-words text-[14px] text-cc-ink">{text}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="mt-2 text-[14px] text-cc-muted">No extra detail for this step.</p>
          )}
        </div>
      ) : null}
    </div>
  )
}
