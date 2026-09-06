import { supabase } from './supabase'

export const API = String(import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(
  /\/$/,
  '',
)

export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${API}${suffix}`
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers)
  if (supabase) {
    const { data } = await supabase.auth.getSession()
    const token = data.session?.access_token
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }
  const url = /^https?:\/\//i.test(path) ? path : apiUrl(path)
  return fetch(url, { ...init, headers })
}

// AC-45/AC-46 (observability PRD §7): frontend-only interactions with no
// natural backend call site. Fire-and-forget — telemetry must never break
// the feature it's attached to, so failures are swallowed, not surfaced.
export function trackEvent(eventName: string, properties?: Record<string, unknown>): void {
  void apiFetch('/api/events', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ event_name: eventName, properties: properties || {} }),
  }).catch(() => {
    // best-effort — a dropped telemetry event is never worth surfacing
  })
}

export async function readError(r: Response, fallback: string): Promise<string> {
  const d = (await r.json().catch(() => ({}))) as { detail?: unknown }
  return typeof d.detail === 'string' ? d.detail : fallback
}

export function fmtUsd(n: number | null | undefined): string {
  const v = Number(n)
  if (!Number.isFinite(v)) return '—'
  if (v < 0.01 && v > 0) return `~$${v.toFixed(3)}`
  return `~$${v.toFixed(2)}`
}
