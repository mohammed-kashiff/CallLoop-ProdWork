import type { TranscriptSegment } from '../types'

export function looksLikeSpeakerDump(text: string): boolean {
  return /\[speaker_\d+\]/i.test(text || '')
}

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

  const agent = agentSpeaker.trim().toLowerCase().replace(/\s+/g, '_')
  const out: TranscriptSegment[] = []
  for (let i = 0; i < marks.length; i++) {
    const start = marks[i].end
    const stop = i + 1 < marks.length ? marks[i + 1].index : raw.length
    const body = raw.slice(start, stop).trim()
    if (!body) continue
    const id = marks[i].speaker
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
  const out: TranscriptSegment[] = []
  let n = 0
  for (const s of segments) {
    const row = asRecord(s)
    const text = asString(row.text)
    const t0 = asNumber(row.start)
    const t1 = asNumber(row.end)
    if (looksLikeSpeakerDump(text)) {
      const parts = parseSpeakerTaggedText(text, agentSpeaker)
      const total = parts.reduce((sum, p) => sum + p.text.length, 0) || 1
      const span = Math.max(0, t1 - t0)
      let cursor = t0
      for (const p of parts) {
        const dur = span * (p.text.length / total)
        out.push({
          ...p,
          id: String(n++),
          start: cursor,
          end: cursor + dur,
        })
        cursor += dur
      }
      continue
    }
    const speaker = asString(row.speaker)
    out.push({
      id: String(row.seq ?? n++),
      speaker: speaker === agentSpeaker ? 'agent' : 'customer',
      start: t0,
      end: t1,
      text,
    })
  }
  return out
}
