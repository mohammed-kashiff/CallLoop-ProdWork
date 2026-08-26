import { fmtUsd } from '../lib/api'
import { usePyaiStatus } from '../context/PyaiStatus'

export function LiveTicker() {
  const { status, label, openKeys } = usePyaiStatus()

  const stats =
    status?.quota_label ||
    [
      status?.pyai_actions != null ? `${status.pyai_actions} PyAI` : null,
      status?.pyai_polls ? `${status.pyai_polls} polls` : null,
      status?.claude_hits != null ? `${status.claude_hits} Claude` : null,
    ]
      .filter(Boolean)
      .join(' · ') ||
    'Waiting…'

  return (
    <div className="live-ticker" aria-label="PyAI usage">
      <button type="button" className="live-pill" onClick={openKeys} title="Key status (host environment)">
        <span
          className={['live-dot', status?.healthy ? 'is-ok' : ''].filter(Boolean).join(' ')}
          aria-hidden="true"
        />
        <strong>{label.toUpperCase()}</strong>
        <span className="live-stats">{stats}</span>
      </button>
      {status?.cost_today && (
        <span
          className="live-today"
          title="Approximate spend today (UTC), not a provider invoice."
        >
          <span className="live-today-kicker">Today</span>
          {fmtUsd(status.cost_today.total_usd)}
        </span>
      )}
    </div>
  )
}
