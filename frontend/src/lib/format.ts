export function formatTime(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds))
  const m = Math.floor(s / 60)
  const r = s % 60
  return `${m}:${r.toString().padStart(2, '0')}`
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

/** Capitalize the first letter (keeps the rest as-is). */
export function capFirst(raw: string | null | undefined): string {
  const s = (raw ?? '').trim()
  if (!s) return ''
  return s.replace(/^([a-z])/, (ch) => ch.toUpperCase())
}

/** Capitalize the first letter of each word (e.g. speaker 2 → Speaker 2). */
export function capWords(raw: string | null | undefined): string {
  const s = (raw ?? '').trim()
  if (!s) return ''
  return s.replace(/\b([a-z])/g, (ch) => ch.toUpperCase())
}

/** Label for a feedback item's sentiment tag (Strength / Improve). Neutral items get no tag. */
export function sentimentLabel(sentiment: string): string | null {
  if (sentiment === 'positive') return 'Strength'
  if (sentiment === 'negative') return 'Improve'
  return null
}

/**
 * Discrete score colors:
 * 90–100 green · 80–89 yellow · 70–79 light orange · 60–69 dark orange · <60 red
 */
export function scoreHue(score: number): string {
  const s = Math.min(100, Math.max(0, Math.round(score)))
  if (s >= 90) return '#16a34a' // green
  if (s >= 80) return '#ca8a04' // yellow
  if (s >= 70) return '#fb923c' // light orange
  if (s >= 60) return '#ea580c' // dark orange
  return '#dc2626' // red
}

