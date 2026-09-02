import type { TranscriptSegment } from '../types'

/** Keep in sync with backend/transcribe.py (_TOKEN_RE / _speech_weight / _piece_spans). */
const TOKEN_RE = /[A-Za-z0-9']+/g
const MIN_PIECE_SEC = 0.35

export function looksLikeSpeakerDump(text: string): boolean {
  return /\[speaker_\d+\]/i.test(text || '')
}

export function stripSpeakerTags(text: string): string {
  return (text || '').replace(/\[speaker_\d+\]/gi, ' ').replace(/\s+/g, ' ').trim()
}

/** Hear may send speaker_1, "1", or 1. Empty agent_speaker must not map everyone to customer. */
export function canonSpeaker(raw: unknown, fallback = ''): string {
  if (typeof raw === 'number' && Number.isFinite(raw)) {
    return `speaker_${raw}`
  }
  const s = String(raw ?? '').trim().toLowerCase().replace(/\s+/g, '_')
  if (!s) return fallback
  const m = s.match(/^(?:speaker[_-]?)?(\d+)$/)
  if (m) return `speaker_${Number(m[1])}`
  return s
}

function normToken(raw: string): string {
  return (raw || '').toLowerCase().replace(/[^a-z0-9']+/g, '')
}

function speechTokens(text: string): string[] {
  return (text.match(TOKEN_RE) || []).map(normToken).filter(Boolean)
}

function speechWeight(text: string): number {
  return Math.max(speechTokens(text).length, 1)
}

function pieceSpans(t0: number, t1: number, weights: number[]): [number, number][] {
  const span = Math.max(0, t1 - t0)
  if (!weights.length) return []
  const total = weights.reduce((sum, w) => sum + w, 0) || 1
  let durs = weights.map((w) => span * (w / total))
  if (span >= MIN_PIECE_SEC * weights.length) {
    durs = durs.map((d) => Math.max(MIN_PIECE_SEC, d))
    const scale = durs.reduce((sum, d) => sum + d, 0) || 1
    durs = durs.map((d) => span * (d / scale))
  }
  const out: [number, number][] = []
  let cursor = t0
  for (const dur of durs) {
    out.push([cursor, cursor + dur])
    cursor += dur
  }
  if (out.length) out[out.length - 1] = [out[out.length - 1][0], t1]
  return out
}

type ExpandRow = TranscriptSegment & { fromTag?: boolean }

export function parseSpeakerTaggedText(
  text: string,
  agentSpeaker = 'speaker_1',
): TranscriptSegment[] {
  const raw = text || ''
  const re = /\[(speaker_\d+)\]/gi
  const marks: { index: number; speaker: string; end: number }[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(raw)) !== null) {
    marks.push({ index: m.index, speaker: m[1].toLowerCase(), end: m.index + m[0].length })
  }
  if (!marks.length) return []

  const agent = canonSpeaker(agentSpeaker, 'speaker_1')
  const out: TranscriptSegment[] = []
  for (let i = 0; i < marks.length; i++) {
    const start = marks[i].end
    const stop = i + 1 < marks.length ? marks[i + 1].index : raw.length
    const body = raw.slice(start, stop).trim()
    if (!body) continue
    const id = canonSpeaker(marks[i].speaker)
    out.push({
      id: `recap-${i}`,
      speaker: id === agent ? 'agent' : 'customer',
      start: 0,
      end: 0,
      text: body,
    })
  }
  return out
}

export function expandTaggedTranscript(
  segments: unknown[],
  agentSpeaker: string,
  asRecord: (v: unknown) => Record<string, unknown>,
  asString: (v: unknown, fallback?: string) => string,
  asNumber: (v: unknown, fallback?: number) => number,
): TranscriptSegment[] {
  const out: ExpandRow[] = []
  let tagN = 0
  let rowN = 0
  const agent = canonSpeaker(agentSpeaker, 'speaker_1')
  for (const s of segments) {
    const row = asRecord(s)
    const text = asString(row.text)
    const t0 = asNumber(row.start)
    const t1 = asNumber(row.end)
    if (looksLikeSpeakerDump(text)) {
      const parts = parseSpeakerTaggedText(text, agentSpeaker)
      const spans = pieceSpans(
        t0,
        t1,
        parts.map((p) => speechWeight(p.text)),
      )
      parts.forEach((p, i) => {
        const [start, end] = spans[i] ?? [t0, t1]
        out.push({
          ...p,
          id: `tag-${tagN++}`,
          start,
          end,
          fromTag: true,
        })
      })
      continue
    }
    const speaker = canonSpeaker(row.speaker) || canonSpeaker(row.channel)
    out.push({
      id: row.seq != null ? `seq-${asNumber(row.seq)}` : `row-${rowN++}`,
      speaker: speaker === agent ? 'agent' : 'customer',
      start: t0,
      end: t1,
      text,
    })
  }
  out.sort((a, b) => a.start - b.start || a.end - b.end)
  let prevEnd = Number.NEGATIVE_INFINITY
  for (const row of out) {
    if (row.fromTag && row.start < prevEnd) {
      row.start = prevEnd
      if (row.end < row.start) row.end = row.start
    }
    prevEnd = Math.max(prevEnd, row.end)
    delete row.fromTag
  }
  return out
}
