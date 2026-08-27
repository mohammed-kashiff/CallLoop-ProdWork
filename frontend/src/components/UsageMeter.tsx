import { usePyaiStatus } from '../context/PyaiStatus'
import { useAuth } from '../context/AuthContext'
import { flagEnabled } from '../lib/features'

export function UsageMeter() {
  const { features } = useAuth()
  const { status, isSandbox, label, openKeys } = usePyaiStatus()
  if (!flagEnabled(features, 'show_usage_bar')) return null
  const title = label || 'PyAI'
  const missing =
    !status ||
    title.toLowerCase() === 'no key' ||
    title.toLowerCase() === 'pyai' ||
    title === '…'
  const tone = isSandbox ? 'sandbox' : missing ? 'pending' : 'live'

  return (
    <button
      type="button"
      className={['usage-meter', `is-${tone}`].join(' ')}
      aria-label={`${title}. Change PyAI and Claude keys`}
      onClick={openKeys}
    >
      <p className="usage-kicker">{title}</p>
      {isSandbox ? (
        <div className="usage-unlimited">
          <span className="usage-infinity" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path
                fill="none"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                d="M4.8 12c0-2.4 1.9-4.3 4.3-4.3 1.9 0 3.1 1.2 4.9 3.3 1.8-2.1 3-3.3 4.9-3.3 2.4 0 4.3 1.9 4.3 4.3s-1.9 4.3-4.3 4.3c-1.9 0-3.1-1.2-4.9-3.3-1.8 2.1-3 3.3-4.9 3.3-2.4 0-4.3-1.9-4.3-4.3Z"
              />
            </svg>
          </span>
          <span>Unlimited</span>
        </div>
      ) : flagEnabled(features, 'show_billed_usage_panel') ? (
        <div className="usage-live-copy">
          <span>
            {missing
              ? title === '…'
                ? 'Checking…'
                : 'Add a PyAI key'
              : 'Billed usage'}
          </span>
        </div>
      ) : null}
      <span className="usage-hint">Click to replace keys</span>
    </button>
  )
}
