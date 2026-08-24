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
  const marks: { index: number; speaker: string }[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(raw)) !== null) {
    marks.push({ index: m.index, speaker: m[1].toLowerCase() })
  }
  if (!marks.length) return []

  const agent = agentSpeaker.trim().toLowerCase().replace(/\s+/g, '_')
  const out: TranscriptSegment[] = []
  for (let i = 0; i < marks.length; i++) {
    const start = marks[i].index + marks[i].speaker.length + 2
    const end = i + 1 < marks.length ? marks[i + 1].index : raw.length
    const body = raw.slice(start, end).trim()
    if (!body) continue
    const id = marks[i].speaker.toLowerCase()
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
