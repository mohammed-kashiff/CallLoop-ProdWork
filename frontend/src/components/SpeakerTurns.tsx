import type { TranscriptSegment } from '../types'

export function SpeakerTurns({ segments }: { segments: TranscriptSegment[] }) {
  if (!segments.length) return null
  return (
    <ul className="recap-turns" aria-label="Agent and customer turns">
      {segments.map((seg) => (
        <li key={seg.id} className={['recap-turn', seg.speaker].join(' ')}>
          <span className="speaker">{seg.speaker === 'agent' ? 'Agent' : 'Customer'}</span>
          <span className="text">{seg.text}</span>
        </li>
      ))}
    </ul>
  )
}
