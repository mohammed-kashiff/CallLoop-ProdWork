export type CcTrailEvent = {
  stage: string
  status: 'started' | 'succeeded' | 'failed'
  created_at: string | null
  error?: string | null
  apis?: { method: string; endpoint: string }[]
  extra?: Record<string, unknown> | null
  agentId?: string | null
}

export function orderTrailEvents(events: CcTrailEvent[]): CcTrailEvent[] {
  return [
    ...events.filter((e) => e.status === 'failed'),
    ...events.filter((e) => e.status !== 'failed'),
  ]
}

export function defaultSelectedIndex(events: CcTrailEvent[]): number {
  if (events.length === 0) return 0
  const fail = events.findIndex((e) => e.status === 'failed')
  return fail >= 0 ? fail : events.length - 1
}

export function trailCounts(events: CcTrailEvent[]): {
  succeeded: number
  failed: number
  started: number
} {
  let succeeded = 0
  let failed = 0
  let started = 0
  for (const e of events) {
    if (e.status === 'succeeded') succeeded += 1
    else if (e.status === 'failed') failed += 1
    else started += 1
  }
  return { succeeded, failed, started }
}

function parseTime(value: string | null): number | null {
  if (!value) return null
  const t = Date.parse(value)
  return Number.isFinite(t) ? t : null
}

export function trailSpan(events: CcTrailEvent[]): {
  first: string | null
  last: string | null
  elapsed: string | null
} {
  const times = events
    .map((e) => parseTime(e.created_at))
    .filter((t): t is number => t != null)
    .sort((a, b) => a - b)
  if (times.length === 0) {
    return { first: null, last: null, elapsed: null }
  }
  const firstMs = times[0]
  const lastMs = times[times.length - 1]
  return {
    first: new Date(firstMs).toLocaleString(),
    last: new Date(lastMs).toLocaleString(),
    elapsed: formatElapsed(lastMs - firstMs),
  }
}

function formatElapsed(ms: number): string {
  if (ms < 0) return '—'
  const s = Math.round(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  const rem = s % 60
  if (m < 60) return rem ? `${m}m ${rem}s` : `${m}m`
  const h = Math.floor(m / 60)
  const mins = m % 60
  return mins ? `${h}h ${mins}m` : `${h}h`
}

export function trailDetailEntries(extra: Record<string, unknown> | null | undefined): [string, string][] {
  if (!extra) return []
  const out: [string, string][] = []
  for (const [key, value] of Object.entries(extra)) {
    if (value == null || value === '') continue
    const text = typeof value === 'string' ? value : JSON.stringify(value)
    if (!text || text === '{}' || text === '[]') continue
    out.push([key, text])
  }
  return out
}
