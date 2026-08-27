import { NavLink } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { flagEnabled } from '../lib/features'

export function PyaiBadge({ onNavigate }: { onNavigate?: () => void }) {
  const { features } = useAuth()
  if (!flagEnabled(features, 'show_powered_by_pyai')) return null
  return (
    <NavLink
      to="/pyai"
      className={({ isActive }) =>
        ['pyai-badge', isActive ? 'is-active' : ''].filter(Boolean).join(' ')
      }
      onClick={onNavigate}
    >
      <span className="pyai-brain" aria-hidden="true">
        <svg viewBox="0 0 24 24">
          <path
            className="pyai-brain-outline"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
            d="M12 4.6c-2.1 0-3.3 1.1-3.9 2.3C7 6.2 5.4 7 5.4 9.1c0 1.1.5 1.9 1.1 2.4-.6.5-1.1 1.2-1.1 2.5 0 1.9 1.5 3.1 3.2 3.1.5 0 1-.1 1.4-.3.6 1.1 1.5 2.1 2 2.1s1.4-1 2-2.1c.4.2.9.3 1.4.3 1.7 0 3.2-1.2 3.2-3.1 0-1.3-.5-2-1.1-2.5.6-.5 1.1-1.3 1.1-2.4 0-2.1-1.6-2.9-2.7-2.2-.6-1.2-1.8-2.3-3.9-2.3Z"
          />
          <path
            className="pyai-brain-fold"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.3"
            strokeLinecap="round"
            d="M12 6.2v11.2M8.2 9.2c.8.4 1.6.3 2.2-.2M8.4 12.4c.9.3 1.7.1 2.3-.4M15.8 9.2c-.8.4-1.6.3-2.2-.2M15.6 12.4c-.9.3-1.7.1-2.3-.4"
          />
          <circle className="pyai-spark is-a" cx="8.2" cy="10.4" r="0.7" />
          <circle className="pyai-spark is-b" cx="15.8" cy="11.2" r="0.7" />
          <circle className="pyai-spark is-c" cx="12" cy="8.4" r="0.65" />
        </svg>
      </span>
      <span>Powered by PyAI</span>
    </NavLink>
  )
}
