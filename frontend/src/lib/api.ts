export const API = String(import.meta.env.VITE_API_URL || 'http://localhost:8000').replace(
  /\/$/,
  '',
)

export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`
  return `${API}${suffix}`
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  return fetch(apiUrl(path), init)
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
