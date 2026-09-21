import { formatTime } from '../lib/format'
import type { CriterionFinding } from '../types'
import { FindingStance, type FindingStanceRow, type FindingStanceValue } from './FindingStance'
import { VerdictBadge } from './VerdictBadge'

interface CriteriaFindingsProps {
  criteria: CriterionFinding[]
  onSeek: (seconds: number) => void
  responses?: Record<string, FindingStanceRow>
  canRespond?: boolean
  busyId?: string | null
  errors?: Record<string, string>
  onRespond?: (dimensionId: string, stance: FindingStanceValue, note?: string) => void
}

export function CriteriaFindings({
  criteria,
  onSeek,
  responses,
  canRespond = false,
  busyId,
  errors,
  onRespond,
}: CriteriaFindingsProps) {
  return (
    <section className="criteria-panel" aria-label="Per-criterion findings">
      <ul className="criteria-list">
        {criteria.map((c) => (
          <li key={c.id} className="criterion">
            <div className="criterion-top">
              <div>
                <h3>
                  {c.name}
                  {c.isGate && <span className="gate-tag">GATE</span>}
                  <span className="check-chip">{c.checkType}</span>
                </h3>
                <p className="criterion-meta">
                  weight {c.weight} · {c.pointsEarned}/{c.pointsPossible} pts
                </p>
              </div>
              <VerdictBadge verdict={c.verdict} />
            </div>
            <p className="criterion-rationale">{c.rationale}</p>
            {c.evidenceQuote && (
              <blockquote className="evidence">
                <p>“{c.evidenceQuote}”</p>
                {c.evidenceTimestamp != null && (
                  <button
                    type="button"
                    className="timestamp-btn"
                    onClick={() => onSeek(c.evidenceTimestamp!)}
                  >
                    Jump to {formatTime(c.evidenceTimestamp)}
                  </button>
                )}
              </blockquote>
            )}
            {!c.evidenceQuote && c.evidenceTimestamp != null && (
              <button
                type="button"
                className="timestamp-btn ghost"
                onClick={() => onSeek(c.evidenceTimestamp!)}
              >
                Related moment {formatTime(c.evidenceTimestamp)}
              </button>
            )}
            {onRespond ? (
              <FindingStance
                current={responses?.[c.id]}
                canRespond={canRespond}
                busy={busyId === c.id}
                error={errors?.[c.id] || null}
                onRespond={(stance, note) => onRespond(c.id, stance, note)}
              />
            ) : null}
          </li>
        ))}
      </ul>
    </section>
  )
}
